"""
Webhook write-back: generate ResourceOverride CR YAMLs and commit them to git.

Output layout (single repo + path, configured in chart values, required):

    <crWriteback.repoUrl>@<crWriteback.branch>:<crWriteback.path>/<namespace>.resource-override.yaml

One file per Namespace. The file holds one ResourceOverride per workload as
separate YAML documents (`---` separated). This is the layout that landed
together with the annotation-only namespace-based opt-in: the unit of
write-back follows the unit of opt-in (the Namespace), not an ArgoCD
abstraction the tool no longer depends on.

Configuration is **required**: chart values `config.crWriteback.{repoUrl, path}`
must be set. The chart's Helm `required` template blocks `helm install` when
either is empty; Config.validate() re-checks at runtime as defence in depth.

All file/git plumbing is shared with src/writeback.py via helper imports.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from typing import TYPE_CHECKING

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from src import log as _log_module
from src.writeback import (
    ResourceBounds,
    _apply_grow_shrink,
    _build_container_resources,
    _enforce_floors,
    _fmt_delta_cpu,
    _fmt_delta_mem,
    _fmt_memory,
    _parse_cpu_m,
    _parse_memory_bytes,
    _project_path_from_url,
    _query_prom_values,
)
from src.git_provider import GitProvider

if TYPE_CHECKING:
    from src.config import Config, CrWritebackConfig, MrConfig
    from src.workload import WorkloadRecommendation

_log = _log_module.get(__name__)


# Suffix for every CR file the tool writes. Every file under crWriteback.path
# matching this suffix is owned by this tool — used both to skip non-tool
# YAMLs (static manifests, etc.) and to clean up files for namespaces that
# stopped opting in (or got renamed by an Application split).
CR_FILE_SUFFIX = ".resource-override.yaml"


# CR API constants — must match the CRD shipped in the helm chart and the
# webhook server's expectations (src/webhook_cache.py).
CR_API_VERSION = "kube-resource-updater.io/v1"
CR_KIND = "ResourceOverride"


# MR description size cap (bytes). GitLab rejects descriptions > 1 MiB with
# a generic 422 "description is too long" mid-sync (commit already pushed,
# MR never opens — silent half-done state). We pre-truncate slightly below
# the limit so encoding overhead never tips us over. The footer message is
# appended after truncation, so the final document stays under the cap even
# after we add it.
_MR_DESCRIPTION_CAP_BYTES = 900_000
# Headroom reserved below the cap for the truncation footer + encoding
# overhead.  The budget is computed cap-relative so providers with a smaller
# cap (e.g. GitHub at 60 kB) still produce a non-empty body after truncation:
#   headroom = min(_MR_DESCRIPTION_HEADROOM_BYTES, cap_bytes // 4)
#   truncate_to = max(cap_bytes - headroom, 0)
# GitLab: cap=900_000 → headroom=min(100_000, 225_000)=100_000 → truncate_to=800_000 (unchanged).
# GitHub: cap=60_000  → headroom=min(100_000,  15_000)= 15_000 → truncate_to=45_000  (non-zero).
_MR_DESCRIPTION_HEADROOM_BYTES = 100_000
# Since 1.22.11 the footer reports how many container rows were
# dropped so the operator opening the MR doesn't assume the visible set is
# complete. `{n}` is filled in by `_truncate_mr_description`.
_MR_DESCRIPTION_TRUNCATION_FOOTER_FMT = (
    "\n\n_(description truncated — {n} more container row(s) not shown; "
    "see commit message / job logs for the full diff)_\n"
)
# Kept for backwards compatibility with any callers that imported the
# constant directly (we did one in qa_params.py — that import is removed
# alongside this change). Synthesized from the format string with n=0 only
# as a literal placeholder; production code uses _MR_DESCRIPTION_TRUNCATION_FOOTER_FMT.
_MR_DESCRIPTION_TRUNCATION_FOOTER = _MR_DESCRIPTION_TRUNCATION_FOOTER_FMT.format(n=0)


@dataclass
class WebhookEntry:
    """One workload's pending CR write — bundle of metadata + computed resources."""
    namespace: str
    cr_name: str          # CR metadata.name == workload name (deployment / statefulset)
    selector_labels: dict[str, str]
    containers: list[dict]   # [{ "name": ..., "requests": {...}, "limits": {...} }]
    # OOM-aware annotations the slow-path bump logic produces — merged
    # form of (live state from apiserver) + (this sync's bumps). Keyed
    # by full annotation key (e.g. `oom-floor.<container>`). Empty when
    # OOM-aware is disabled or this workload had no events.
    oom_annotations: dict[str, str] = field(default_factory=dict)
    # Routing decision resolved per workload through the helm < ns < workload
    # hierarchy. True → this entry's diff opens (or updates) a Merge Request.
    # False → push direct to the target branch. previously
    # `cmd_sync` ignored `eff.create_mr` and used the global flag for every
    # workload. With per-entry routing, a single sync can produce two
    # parallel pushes (one direct, one MR) per repo.
    create_mr: bool = True
    # Per-workload dry-run flag, resolved through the same helm < ns < workload
    # hierarchy as create_mr. True → this entry is logged as "[DRY RUN]" but
    # EXCLUDED from the git write. Previously only the cluster-wide
    # global dry_run param was honoured; a namespace annotation
    # `kube-resource-updater.dryRun: "true"` resolved into Config.dry_run but
    # was never consumed per-workload, so the CR was still written silently.
    dry_run: bool = False


# --------------------------------------------------------------------------- #
# Public entrypoint                                                            #
# --------------------------------------------------------------------------- #

def write_back_webhook_all(
    workloads_with_configs: list[tuple[WorkloadRecommendation, Config]],
    cr_writeback: CrWritebackConfig,
    provider: GitProvider,
    git_author_name: str,
    git_author_email: str,
    dry_run: bool = False,
    mr_config: MrConfig | None = None,
    oom_state_lookup: dict[tuple[str, str], dict] | None = None,
    oom_events_lookup: dict[tuple[str, str, str], OomEvent] | None = None,
    oom_eligibility_lookup: dict[tuple[str, str], bool] | None = None,
    oom_floor_enabled_lookup: dict[tuple[str, str], bool] | None = None,
    oom_floor_reset_lookup: dict[tuple[str, str], bool] | None = None,
    auto_rollout_by_namespace: dict[str, bool] | None = None,
) -> list[tuple[str, list[str]]] | None:
    """Generate ResourceOverride CR files and commit them to the configured central repo.

    `workloads_with_configs` is the output of the resolver: each workload has its
    own effective `Config` already (helm defaults + namespace annotations + workload
    annotations applied), including `create_mr`. The write-back layer doesn't
    re-resolve anything — it just turns the per-workload Config +
    WorkloadRecommendation into a CR and routes the result through the right
    bucket (direct push vs MR) based on the entry's per-workload `create_mr`.

    `oom_state_lookup` / `oom_events_lookup` / `oom_eligibility_lookup`
    feed the slow-path OOM-aware bump. All three default
    to empty/None when the feature is disabled cluster-wide; the build
    pipeline handles missing state gracefully (no bump, no annotation
    churn).

    Returns a list of (result_url, [namespace, ...]) — up to TWO entries per
    sync now, one per bucket (direct push + MR) that actually produced work.
    None on dry-run or no-op.
    """
    if not workloads_with_configs:
        return None

    # Compute the CR payloads up front (no git I/O yet). This means dry-run can show
    # the full set of files that *would* be written without cloning anything.
    entries = _build_entries(
        workloads_with_configs,
        oom_state_lookup=oom_state_lookup,
        oom_events_lookup=oom_events_lookup,
        oom_eligibility_lookup=oom_eligibility_lookup,
        oom_floor_enabled_lookup=oom_floor_enabled_lookup,
        oom_floor_reset_lookup=oom_floor_reset_lookup,
    )

    # Per-entry dry-run: an entry is dry-run when its own flag is set OR when
    # the global cluster-wide dry_run override is True. The global param is
    # belt-and-suspenders — the resolver already merged the helm default into
    # each pair's Config.dry_run, so the per-entry value inherits the cluster
    # default. Keeping the global OR preserves the existing --dry-run CLI /
    # cluster-ConfigMap behavior (set once, covers everything).
    dry_entries = [e for e in entries if e.dry_run or dry_run]
    write_entries = [e for e in entries if not (e.dry_run or dry_run)]

    if dry_entries:
        direct_dry_n = sum(1 for e in dry_entries if not e.create_mr)
        mr_dry_n = len(dry_entries) - direct_dry_n
        _log.info(
            "[DRY RUN] webhook mode — %d entry(ies) direct push, %d entry(ies) via MR"
            " (excluded from git write)",
            direct_dry_n, mr_dry_n,
        )
        for e in dry_entries:
            _log.info(
                "[DRY RUN] ns=%s  cr=%s  containers=%d  mode=%s  file=%s",
                e.namespace, e.cr_name, len(e.containers),
                "MR" if e.create_mr else "direct",
                _cr_relpath(e, cr_writeback.path),
            )

    if not write_entries:
        return None

    repo_url = cr_writeback.repo_url
    branch = cr_writeback.branch or "main"

    results = _commit_repo(
        repo_url=repo_url,
        branch=branch,
        path=cr_writeback.path,
        entries=write_entries,
        provider=provider,
        git_author_name=git_author_name,
        git_author_email=git_author_email,
        mr_config=mr_config,
        auto_rollout_by_namespace=auto_rollout_by_namespace,
    )
    return results or None


# --------------------------------------------------------------------------- #
# Build phase (no I/O)                                                         #
# --------------------------------------------------------------------------- #

def _build_entries(
    workloads_with_configs: list[tuple[WorkloadRecommendation, Config]],
    *,
    oom_state_lookup: dict[tuple[str, str], dict] | None = None,
    oom_events_lookup: dict[tuple[str, str, str], OomEvent] | None = None,
    oom_eligibility_lookup: dict[tuple[str, str], bool] | None = None,
    oom_floor_enabled_lookup: dict[tuple[str, str], bool] | None = None,
    oom_floor_reset_lookup: dict[tuple[str, str], bool] | None = None,
) -> list[WebhookEntry]:
    """Compute every CR's container payload from per-workload Config.

    The caller (cmd_sync) is responsible for filtering out workloads whose
    effective Config has both grow_only and shrink_only set — a logic conflict
    that would produce no value changes regardless. Here we trust the input.

    OOM-aware:
      - `oom_state_lookup`: live state per CR from apiserver (`fetch_oom_state`).
      - `oom_events_lookup`: detected OOMKilled events per `(ns, workload, container)`.
      - `oom_eligibility_lookup`: per-`(ns, workload)` boolean (helm < ns < workload).
    """
    out: list[WebhookEntry] = []
    state_lookup = oom_state_lookup or {}
    events_lookup = oom_events_lookup or {}
    eligibility_lookup = oom_eligibility_lookup or {}
    floor_enabled_lookup = oom_floor_enabled_lookup or {}
    floor_reset_lookup = oom_floor_reset_lookup or {}

    # Pre-scan for (namespace, target_name) collisions across kinds.
    # Kubernetes lets a Deployment and a StatefulSet share a name in the
    # same namespace; the CR CRD does NOT (CR is namespace-scoped, name is
    # unique per kind per namespace, but we write all workload kinds to
    # the SAME CR kind = ResourceOverride). Without disambiguation, two
    # WebhookEntry objects share `cr_name`, two CR docs in the same file
    # collide, and apiserver rejects the second on apply. Detected here
    # so the non-collision case (99%+ of installs) keeps the bare
    # workload-name as `cr_name` and only the affected pair gets
    # disambiguated with a kind prefix.
    collision_keys: set[tuple[str, str]] = set()
    _seen_by_workload: dict[tuple[str, str], list[str]] = {}
    for rec, _ in workloads_with_configs:
        key = (rec.namespace, rec.target_name)
        _seen_by_workload.setdefault(key, []).append(rec.target_kind)
    for key, kinds in _seen_by_workload.items():
        if len(kinds) > 1:
            collision_keys.add(key)
            ns, name = key
            _log.warning(
                "[cr-name-collision] %s: two workloads share name %r (%s) — "
                "CR names will be disambiguated with kind prefix "
                "(e.g. 'deployment-%s', 'statefulset-%s'). Operators with "
                "selectors hardcoded against the bare CR name will need to "
                "update. To avoid the prefix, rename one workload.",
                ns, name, " + ".join(sorted(kinds)), name, name,
            )

    for rec, cfg in workloads_with_configs:
        wl_key = (rec.namespace, rec.target_name)
        cr_name = (
            f"{rec.target_kind.lower()}-{rec.target_name}"
            if wl_key in collision_keys
            else rec.target_name
        )
        # k8s name limit is 63 chars per RFC 1123.
        # Without a guard, `kubectl apply -f <cr.yaml>` rejects the CR
        # with "invalid metadata.name: must be no more than 63
        # characters". The collision-prefix path (`deployment-<name>`
        # adds 11 chars) makes this easy to trip for workloads with
        # 53+ char names. Skip with a clear warning so the operator
        # sees the issue (instead of ArgoCD reporting a per-sync CR
        # apply failure with the same message every reconcile loop).
        if len(cr_name) > 63:
            _log.warning(
                "[cr-name-too-long] %s/%s: derived CR name %r is %d chars, "
                "exceeds k8s 63-char limit. Workload skipped — rename the %s "
                "(target_name=%r) to fit. Affects collisions with the "
                "kind-prefix (e.g. 'deployment-<name>') most often.",
                rec.namespace, rec.target_name, cr_name, len(cr_name),
                rec.target_kind.lower(), rec.target_name,
            )
            continue
        # The oom_state_lookup is keyed by (ns, cr_name) — since fetch_oom_state
        # reads CR names from apiserver, we need to look up using THE SAME
        # name we'll write. For colliding workloads the apiserver may have
        # a stale CR under the bare name (pre-collision) — that's orphaned
        # automatically by the prune-orphan pass.
        oom_state = state_lookup.get((rec.namespace, cr_name))
        # Bucket events for THIS workload — keyed by container name only.
        wl_events: dict[str, OomEvent] = {
            container: ev
            for (ns, name, container), ev in events_lookup.items()
            if (ns, name) == wl_key
        }
        # Default to feature-on; main.cmd_sync is responsible for resolving
        # per-workload eligibility upstream.
        eligible = eligibility_lookup.get(wl_key, True)
        floor_enabled = floor_enabled_lookup.get(wl_key, True)
        floor_reset = floor_reset_lookup.get(wl_key, False)

        containers_payload, oom_annotations = _build_containers_payload(
            rec, cfg,
            oom_state=oom_state,
            oom_events=wl_events,
            oom_eligible=eligible,
            oom_floor_enabled=floor_enabled,
            oom_floor_reset=floor_reset,
        )
        if not containers_payload:
            continue
        out.append(WebhookEntry(
            namespace=rec.namespace,
            cr_name=cr_name,
            selector_labels=_derive_selector(rec),
            containers=containers_payload,
            oom_annotations=oom_annotations,
            # Per-workload routing. `cfg.create_mr` is the EFFECTIVE value
            # (resolver already merged helm < ns < workload). A single
            # sync can produce two buckets for the same repo: workloads
            # whose operator opted out of review (false) push direct,
            # everyone else opens a Merge Request.
            create_mr=cfg.create_mr,
            # Per-workload dry-run. `cfg.dry_run` is the EFFECTIVE value
            # (resolver merged helm < ns < workload).
            dry_run=cfg.dry_run,
        ))
    return out


def fetch_oom_floors(
    custom_objects_api,
    namespaces: list[str],
) -> dict[tuple[str, str], dict[str, int]]:
    """List ResourceOverride CRs across the given namespaces and pre-parse
    the `oom-floor` annotation per CR.

    Returns `{(namespace, cr_name): {container: bytes}}`. CRs without
    the annotation are absent from the map. Errors per namespace are
    swallowed with a warning so the slow-path still runs end-to-end on
    a partially-degraded apiserver — the worst-case is "no floor
    enforced for some namespaces this cycle," not "sync fails."

    Called once per `cmd_sync` cycle, before `write_back_webhook_all`.
    """
    from kubernetes.client.rest import ApiException
    out: dict[tuple[str, str], dict[str, int]] = {}
    for ns in sorted(set(namespaces)):
        try:
            result = custom_objects_api.list_namespaced_custom_object(
                group="kube-resource-updater.io",
                version="v1",
                namespace=ns,
                plural="resourceoverrides",
            )
        except ApiException as exc:
            _log.warning(
                "[oom-floor] failed to list ResourceOverrides in %s: %s — "
                "slow-path will skip floor enforcement for this namespace.",
                ns, exc,
            )
            continue
        for cr in result.get("items") or []:
            meta = cr.get("metadata") or {}
            cr_name = meta.get("name")
            if not cr_name:
                continue
            anns = meta.get("annotations") or {}
            floor_map = parse_oom_floors_from_annotations(anns)
            if floor_map:
                out[(ns, cr_name)] = floor_map
    return out


def fetch_oom_state(
    custom_objects_api,
    namespaces: list[str],
) -> dict[tuple[str, str], dict]:
    """Bundle apiserver-source CR state per workload.

    Returns `{(ns, cr_name): {"floor":       {container: bytes},
                              "last_event":  {container: rfc3339},
                              "history":     {container: str},
                              "annotations": {full annotations dict},
                              "labels":      {full labels dict},
                              "containers":  {container_name: {"requests": {...}, "limits": {...}}}}}`.

    Used for:
      - `_render_cr_doc` carry-forward (annotations + labels survive rebuild)
      - `_apply_oom_bump` dedupe (last_event compared against fresh OomEvent.finished_at)
      - `_apply_grow_shrink` (containers feed the per-workload prev_res lookup
        so grow/shrink can compare against what's currently in the apiserver)
    """
    from kubernetes.client.rest import ApiException
    out: dict = {}
    for ns in sorted(set(namespaces)):
        try:
            result = custom_objects_api.list_namespaced_custom_object(
                group="kube-resource-updater.io",
                version="v1",
                namespace=ns,
                plural="resourceoverrides",
            )
        except ApiException as exc:
            _log.warning(
                "[oom] failed to list ResourceOverrides in %s: %s — "
                "skipping floor enforcement / dedupe / grow-shrink for this namespace.",
                ns, exc,
            )
            continue
        for cr in result.get("items") or []:
            meta = cr.get("metadata") or {}
            cr_name = meta.get("name")
            if not cr_name:
                continue
            anns = meta.get("annotations") or {}
            # Per-container res snapshot from the live CR — the source of
            # truth for "what new pods are admitted with right now."
            # Apiserver wins over git for grow/shrink because:
            #   (1) it reflects the state ArgoCD has actually applied,
            #   (2) a hand-patched CR (operator override with selfHeal off)
            #       gets respected as the operator's source of truth,
            #   (3) MR-pending diffs in git haven't taken effect yet and
            #       shouldn't bias the grow/shrink decision.
            containers: dict = {}
            spec = cr.get("spec") or {}
            for c in spec.get("containers") or []:
                cname = c.get("name")
                if not cname:
                    continue
                containers[cname] = {
                    "requests": dict(c.get("requests") or {}),
                    "limits":   dict(c.get("limits") or {}),
                }
            out[(ns, cr_name)] = {
                "floor": parse_oom_floors_from_annotations(anns),
                "last_event": parse_oom_last_events_from_annotations(anns),
                "history": parse_oom_boost_history_from_annotations(anns),
                # Sticky investigation-required flag per
                # container, set after `_OOM_BUMPS_BEFORE_INVESTIGATION`
                # consecutive bumps. Cleared by `oomFloorReset: true`.
                "investigation": parse_oom_investigation_from_annotations(anns),
                "annotations": dict(anns),
                "labels": dict(meta.get("labels") or {}),
                "containers": containers,
            }
    return out


def _filter_skipped_containers(
    rec: WorkloadRecommendation,
) -> tuple[list, list[str]]:
    """Return (kept_containers, dropped_names).

    Reads `rec.skip_containers` (resolved upstream by main.cmd_sync) and
    drops every ContainerRecommendation whose name is in the set. Init
    containers were never put on `rec.containers` to begin with — see
    `src/workload.list_workloads_in_namespace` — so they don't need a
    second filter pass here.
    """
    skip_set = {n for n in (getattr(rec, "skip_containers", None) or []) if n}
    if not skip_set:
        return list(rec.containers), []
    kept: list = []
    dropped: list[str] = []
    for c in rec.containers:
        if c.container_name in skip_set:
            dropped.append(c.container_name)
        else:
            kept.append(c)
    return kept, dropped


# Per-container OOM annotation prefixes. Each container
# gets its OWN annotation per concept — `oom-floor.<container>`,
# `oom-last-event.<container>`, `oom-boost-history.<container>`.
# Operators get one line per concept per container in `kubectl describe`,
# and `kubectl get -o jsonpath='{...oom-floor.web}'` is a direct lookup.
#
# Migration:  used a single CSV-format key per concept
# (`oom-floor: "web=900Mi,cache=64Mi"`). The parsers below read both
# formats; renderers always emit the new per-container shape, so legacy
# CSVs migrate organically on the first sync after upgrade.
OOM_FLOOR_PREFIX = "kube-resource-updater.io/oom-floor."
OOM_LAST_EVENT_PREFIX = "kube-resource-updater.io/oom-last-event."
OOM_BOOST_HISTORY_PREFIX = "kube-resource-updater.io/oom-boost-history."
# Runaway-bump cap (since 1.22.11). After this many bumps recorded
# in the history annotation, the workload is presumed to have a structural
# memory bug (leak / misconfigured GC / wrong sizing model). Stop bumping;
# stamp `oom-investigation-required.<container>: "true"` so the operator
# sees the workload needs human attention. Sticky until cleared via
# `oomFloorReset: "true"` annotation (same operator escape hatch that
# clears floor / last-event / history).
OOM_INVESTIGATION_PREFIX = "kube-resource-updater.io/oom-investigation-required."

# Legacy single-key forms.
_LEGACY_OOM_FLOOR_KEY = "kube-resource-updater.io/oom-floor"
_LEGACY_OOM_LAST_EVENT_KEY = "kube-resource-updater.io/oom-last-event"
_LEGACY_OOM_BOOST_HISTORY_KEY = "kube-resource-updater.io/oom-boost-history"

# Compatibility alias — pre-1.11.0 callers referenced this constant.
OOM_BOOST_HISTORY_KEY = _LEGACY_OOM_BOOST_HISTORY_KEY

_OOM_HISTORY_CAP = 10
# Runaway-bump cap. After 5 bumps the operator is auto-
# bumping a real bug. Stop and stamp investigation-required. Must be
# strictly less than `_OOM_HISTORY_CAP` so history always retains the
# evidence the operator needs to diagnose.
_OOM_BUMPS_BEFORE_INVESTIGATION = 5


def _is_oom_annotation(key: str) -> bool:
    """True if the annotation key is one of the OOM-aware tool's keys
    (per-container prefix form OR legacy single-key form). Used by the
    slow-path's `_render_cr_doc` to know which annotations the tool
    owns vs operator-added customs that must be carried forward
    untouched.
    """
    return (
        key.startswith(OOM_FLOOR_PREFIX)
        or key.startswith(OOM_LAST_EVENT_PREFIX)
        or key.startswith(OOM_BOOST_HISTORY_PREFIX)
        or key in (_LEGACY_OOM_FLOOR_KEY, _LEGACY_OOM_LAST_EVENT_KEY,
                   _LEGACY_OOM_BOOST_HISTORY_KEY)
    )


def _format_oom_boost_entry(boost: dict) -> str:
    """One history line for ONE container: `<RFC3339Z> <from>→<to> (×<factor>)`.

    Per-container annotations keep history per container,
    so each entry is a single container's bump. Caller assembles full
    multi-line history via `_build_oom_boost_history`.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{ts} {boost['from']}→{boost['to']} (×{boost['bump_factor']:g})"


def _parse_legacy_csv(annotation_value: str, value_parser=lambda v: v) -> dict[str, str]:
    """Parse the  single-key CSV format `c=v,c=v` to a dict.

    `value_parser` lets callers convert values during parse (e.g. memory
    quantity → bytes). On any per-entry failure, the entry is dropped.
    """
    out: dict = {}
    for entry in (annotation_value or "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, raw = entry.split("=", 1)
        name = name.strip()
        raw = raw.strip()
        if not name or not raw:
            continue
        try:
            out[name] = value_parser(raw)
        except (ValueError, TypeError):
            # Malformed CSV entry — value didn't parse cleanly. Drop silently;
            # the next sync regenerates from apiserver state, so the bad
            # entry never sticks. Wider exceptions (AttributeError on a
            # surprise type, etc) should surface — they indicate a bug.
            continue
    return out


def parse_oom_floors_from_annotations(annotations: dict[str, str] | None) -> dict[str, int]:
    """Read OOM floor entries from a CR's annotations dict.

    Returns `{container: bytes}`. Reads BOTH the per-container prefix
    format AND the single-key CSV format. Per-container entries take precedence on collision.
    Malformed entries are skipped silently.
    """
    if not annotations:
        return {}
    out: dict[str, int] = {}
    legacy = annotations.get(_LEGACY_OOM_FLOOR_KEY)
    if legacy:
        out.update(_parse_legacy_csv(legacy, _parse_memory_bytes))
    for k, v in annotations.items():
        if not k.startswith(OOM_FLOOR_PREFIX):
            continue
        container = k[len(OOM_FLOOR_PREFIX):]
        if not container:
            continue
        try:
            out[container] = _parse_memory_bytes(v)
        except (ValueError, TypeError):
            continue
    return out


def parse_oom_last_events_from_annotations(annotations: dict[str, str] | None) -> dict[str, str]:
    """Read per-container `finishedAt` dedupe keys.

    Same dual-format support as `parse_oom_floors_from_annotations`.
    Values are kept as RFC3339 strings (cheap lex compare).
    """
    if not annotations:
        return {}
    out: dict[str, str] = {}
    legacy = annotations.get(_LEGACY_OOM_LAST_EVENT_KEY)
    if legacy:
        out.update(_parse_legacy_csv(legacy))
    for k, v in annotations.items():
        if not k.startswith(OOM_LAST_EVENT_PREFIX):
            continue
        container = k[len(OOM_LAST_EVENT_PREFIX):]
        if container and v:
            out[container] = v
    return out


def parse_oom_boost_history_from_annotations(annotations: dict[str, str] | None) -> dict[str, str]:
    """Read per-container OOM boost history (multi-line strings).

    Same dual-format support, but the legacy single-key CSV is rendered
    as one giant multi-line block, so we DO NOT parse it as CSV — we
    keep it under a sentinel key `__legacy__` so the migration write
    path knows to drop it. Operators rarely care; the new per-container
    history starts fresh post-migration.
    """
    if not annotations:
        return {}
    out: dict[str, str] = {}
    for k, v in annotations.items():
        if not k.startswith(OOM_BOOST_HISTORY_PREFIX):
            continue
        container = k[len(OOM_BOOST_HISTORY_PREFIX):]
        if container and v:
            out[container] = v
    return out


def floor_annotation_key(container: str) -> str:
    """`OOM_FLOOR_PREFIX + container` — call this rather than concatenating
    by hand, so the prefix can change in one place.
    """
    return f"{OOM_FLOOR_PREFIX}{container}"


def last_event_annotation_key(container: str) -> str:
    return f"{OOM_LAST_EVENT_PREFIX}{container}"


def history_annotation_key(container: str) -> str:
    return f"{OOM_BOOST_HISTORY_PREFIX}{container}"


def investigation_annotation_key(container: str) -> str:
    """Annotation key for the runaway-bump investigation flag."""
    return f"{OOM_INVESTIGATION_PREFIX}{container}"


def parse_oom_investigation_from_annotations(
    annotations: dict[str, str] | None,
) -> dict[str, bool]:
    """Round-trip read for `oom-investigation-required.<container>` annotations.

    Returns `{container_name: True}` for every container the operator
    should investigate. Empty / None annotations → empty dict. Values
    other than the truthy strings (`"true"`, `"1"`, `"yes"`) are ignored
    — operator can clear via `oomFloorReset: true`, not by setting
    investigation-required to `"false"` (avoids accidental clears from
    tooling that emits empty values).
    """
    out: dict[str, bool] = {}
    if not annotations:
        return out
    for key, value in annotations.items():
        if not key.startswith(OOM_INVESTIGATION_PREFIX):
            continue
        container = key[len(OOM_INVESTIGATION_PREFIX):]
        if not container:
            continue
        if str(value).lower() in ("true", "1", "yes"):
            out[container] = True
    return out


def _count_history_entries(prev_history: str) -> int:
    """Count non-blank lines in an `oom-boost-history.<container>` value."""
    if not prev_history:
        return 0
    return sum(1 for line in prev_history.splitlines() if line.strip())


def _build_oom_boost_history(prev_history: str, new_entry: str) -> str:
    """Prepend a new boost entry to the history string, capped at 10 lines.

    `prev_history` is the existing annotation value (may be empty / missing).
    Newest line goes first so `kubectl describe` shows the latest sync's
    boost on top. Older lines beyond the cap are dropped (FIFO).
    """
    lines = [new_entry]
    if prev_history:
        for line in prev_history.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
    return "\n".join(lines[:_OOM_HISTORY_CAP])


def _log_grow_shrink_clamp(
    rec: WorkloadRecommendation,
    container: str,
    pre: dict,
    post: dict,
    grow_only: bool,
    shrink_only: bool,
) -> None:
    """One log line per container when grow/shrink policy actually clamped a value.

    Skipped when pre == post (no clamp applied). When a clamp does apply,
    the operator should see exactly which field and direction was held
    back, plus the policy flag responsible and how to disable it.
    """
    pre_reqs = (pre or {}).get("requests") or {}
    pre_lims = (pre or {}).get("limits") or {}
    post_reqs = (post or {}).get("requests") or {}
    post_lims = (post or {}).get("limits") or {}

    diffs: list[str] = []
    for section_name, p, q in [("req", pre_reqs, post_reqs), ("lim", pre_lims, post_lims)]:
        for key in ("cpu", "memory"):
            if key in p and p.get(key) != q.get(key):
                # The section is in pre but the value differs in post → clamped.
                # Format: "cpu_req 100m → 250m (held by growOnly)" or similar.
                direction = "growOnly" if grow_only else "shrinkOnly"
                diffs.append(f"{key}_{section_name} {p[key]} → {q.get(key, '—')} ({direction})")
    if not diffs:
        return
    flag = "growOnly" if grow_only and not shrink_only else (
        "shrinkOnly" if shrink_only and not grow_only else "growOnly+shrinkOnly"
    )
    _log.info(
        "  [grow-shrink-clamp] %s/%s/%s: %s — Prom recommended value held by %s; "
        "remove the annotation to allow the change.",
        rec.namespace, rec.target_name, container,
        "  ".join(diffs),
        flag,
    )


def _build_containers_payload(
    rec: WorkloadRecommendation,
    cfg: Config,
    *,
    oom_state: dict | None = None,
    oom_events: dict[str, OomEvent] | None = None,
    oom_eligible: bool = True,
    oom_floor_enabled: bool = True,
    oom_floor_reset: bool = False,
) -> tuple[list[dict], dict[str, str]]:
    """Compute requests/limits for every container; apply OOM bump + floor + grow/shrink.

    Returns `(payload, oom_annotations)`:
      - `payload`: the `containers[]` list for the CR's spec.
      - `oom_annotations`: the per-container OOM annotation keys this
        sync should stamp on the CR (`oom-floor.<c>` / `oom-last-event.<c>`
        / `oom-boost-history.<c>`). Empty when the workload had no
        relevant events and no prior state to migrate.

    Inputs:
      - `cfg`: per-workload **effective** Config (resolver-merged through
        helm < ns < workload). `cfg.grow_only`, `cfg.shrink_only`, all
        bounds, percentile, margins, OOM toggles already reflect the
        hierarchy. The caller (`_build_entries`) is responsible for
        producing the resolved Config; this function does not re-resolve.
      - `oom_state`: live state for THIS CR from the apiserver
        (`fetch_oom_state`'s entry — see that function's docstring for
        keys). `containers` sub-map is also used as old_res for grow/shrink
        clamping. `None` / missing means no prior CR (first sync for this
        workload) — grow/shrink no-ops because there's no baseline.
      - `oom_events`: detected OOM events for this workload's containers
        keyed by container name. Each is processed with dedupe
        (`finished_at > last_event` from prior state) before bumping.
      - `oom_eligible`: result of `is_oom_detection_enabled` resolved
        through helm < ns < workload chain. When False, we don't bump
        but DO carry forward existing floor as the memory minimum
        (operator opted-out new bumps but historical floors stay
        respected — explicit removal goes through annotation deletion).
    """
    # Apiserver-source old_res for grow/shrink. Each container's previously
    # written `requests` / `limits` from the live CR — that's what new pods
    # admit with right now, the right baseline for "did this value change"
    # comparisons. None on first sync for the workload (no prior CR exists)
    # → grow/shrink no-ops, which is correct (nothing to compare against).
    prev_res_lookup: dict[str, dict] = (oom_state or {}).get("containers") or {}

    # Both flags active = freeze. Log loudly per workload — operator should
    # see this in every sync until they remove one flag (hopefully they
    # didn't set both by accident).
    if cfg.grow_only and cfg.shrink_only:
        _log.warning(
            "  [freeze] %s/%s: growOnly + shrinkOnly both active — workload frozen "
            "at current CR values (only floors/ceilings can still change values; "
            "OOM bumps will be suppressed by shrinkOnly with a separate warning). "
            "Remove one flag to resume normal operation.",
            rec.namespace, rec.target_name,
        )
    rc = cfg.resource
    bounds = ResourceBounds.from_config(
        cpu_cap_mult=rc.cpu_limit_multiplier,
        mem_cap_mult=rc.memory_limit_multiplier,
        min_cpu_limit_m=cfg.min_cpu_limit_m,
        min_memory_limit_mi=cfg.min_memory_limit_mi,
        rc=rc,
    )

    # `Config.validate` only checks the HELM-LEVEL
    # min/max bounds at startup. The per-workload resolver merge
    # (helm < ns < workload) can produce an effective config where
    # `min > max` for the same dimension — the silent-clamping order in
    # `_build_container_resources` (apply min, then apply max) would let
    # max win and produce a value BELOW the stated min. Detect here and
    # SKIP the workload with a clear warning so the operator sees the
    # bad combination rather than a silently-out-of-bounds CR.
    bound_pairs = [
        ("CpuRequest",    bounds.min_cpu_request_m,     bounds.max_cpu_request_m),
        ("MemoryRequest", bounds.min_memory_request_mi, bounds.max_memory_request_mi),
        ("CpuLimit",      bounds.min_cpu_limit_m,       bounds.max_cpu_limit_m),
        ("MemoryLimit",   bounds.min_memory_limit_mi,   bounds.max_memory_limit_mi),
    ]
    for name, min_v, max_v in bound_pairs:
        if min_v and max_v and min_v > max_v:
            _log.warning(
                "  [bounds] %s/%s: effective min%s (%d) > max%s (%d) after "
                "helm<ns<workload merge — workload skipped to avoid silent "
                "out-of-bounds CR. Remove the conflicting annotation (likely "
                "on the workload, lowest in the override chain).",
                rec.namespace, rec.target_name, name, min_v, name, max_v,
            )
            return [], {}

    kept, dropped = _filter_skipped_containers(rec)
    if dropped:
        _log.info(
            "  %s/%s skipping containers: %s",
            rec.namespace, rec.target_name, ", ".join(dropped),
        )
    # `skipContainers` listing every container in the workload silently
    # un-manages it (downstream `_build_entries` drops the workload with
    # an empty payload → ArgoCD prunes the CR → admission stops
    # patching). Operator-surprising: they wanted "ignore container X"
    # but got "stop managing this workload entirely" — that's what
    # `skip: "true"` is for. Warn so it's discoverable without
    # `kubectl get ro -A` diffing.
    if not kept and dropped:
        _log.warning(
            "  [skip-containers] %s/%s: ALL containers (%s) filtered by "
            "skipContainers — workload effectively unmanaged (equivalent to "
            "`kube-resource-updater.skip: \"true\"`). To manage some "
            "containers, remove names from skipContainers; to fully unmanage, "
            "use the skip annotation explicitly.",
            rec.namespace, rec.target_name, ", ".join(dropped),
        )

    prior_floor: dict[str, int] = (oom_state or {}).get("floor") or {}
    prior_last_event: dict[str, str] = (oom_state or {}).get("last_event") or {}
    prior_history: dict[str, str] = (oom_state or {}).get("history") or {}
    prior_investigation: dict[str, bool] = (oom_state or {}).get("investigation") or {}
    events: dict[str, OomEvent] = oom_events or {}

    # One-shot reset: operator marked the workload/ns
    # with `kube-resource-updater.oomFloorReset: "true"` to drop sticky
    # state from previous OOMs. Zero out the prior maps so this sync
    # writes a CR with no `oom-*` annotations and any fresh OOM event
    # is treated like a brand-new occurrence (no dedupe collision with
    # the deleted last-event timestamp). Also clears the investigation
    # flag (1.22.11) — operator has acknowledged the leak and wants a
    # fresh bump cycle.
    if oom_floor_reset and (prior_floor or prior_last_event or prior_history or prior_investigation):
        _log.info(
            "  [oom-reset] %s/%s: clearing oom-floor / oom-last-event / "
            "oom-boost-history / oom-investigation-required (operator opt-in "
            "via oomFloorReset annotation; remove the annotation to avoid "
            "re-resetting on every sync)",
            rec.namespace, rec.target_name,
        )
        prior_floor = {}
        prior_last_event = {}
        prior_history = {}
        prior_investigation = {}

    # Final per-container OOM state, post-this-sync. Initialized from
    # prior state; bump logic mutates in place.
    out_floor = dict(prior_floor)
    out_last_event = dict(prior_last_event)
    out_history = dict(prior_history)
    out_investigation = dict(prior_investigation)

    payload: list[dict] = []
    for c in kept:
        # Prometheus values are passed as scalar overrides to _build_container_resources;
        # any of them being None means "fall back to multipliers/VPA target".
        prom = _query_prom_values(c, rec, cfg.prometheus_url, rc) if cfg.prometheus_url else None

        res = _build_container_resources(
            c, bounds,
            cpu_request_m=prom.cpu_request_m if prom else None,
            memory_request_bytes=prom.memory_request_bytes if prom else None,
            cpu_limit_m=prom.cpu_limit_m if prom else None,
            memory_limit_bytes=prom.memory_limit_bytes if prom else None,
        )
        if not res or not res.get("requests"):
            # Normally we skip — without Prom data the webhook leaves the
            # container alone. EXCEPT when a fresh OOM event exists: the
            # workload would loop OOM forever waiting for Prom history.
            # Synthesize minimal resources from floors + the OOM trap so
            # the bump path below can break the loop.
            ev = events.get(c.container_name)
            stored = prior_last_event.get(c.container_name, "")
            fresh_oom = oom_eligible and ev is not None and ev.finished_at > stored
            if not fresh_oom:
                continue
            cpu_floor_m = max(bounds.min_cpu_request_m, rc.cold_start_cpu_floor_m)
            cpu_cap_m = max(
                int(round(cpu_floor_m * bounds.cpu_cap_mult)),
                bounds.min_cpu_limit_m,
                cpu_floor_m,
            )
            mem_req_b = max(bounds.min_memory_request_mi * (1 << 20), 1)
            # `_enforce_floors` only re-checks lim>=req when it changed
            # something else, so we must hold that invariant ourselves.
            mem_lim_b = max(ev.trap_limit_bytes, mem_req_b)
            res = {
                "requests": {"cpu": f"{cpu_floor_m}m", "memory": _fmt_memory(str(mem_req_b))},
                "limits": {"cpu": f"{cpu_cap_m}m", "memory": _fmt_memory(str(mem_lim_b))},
            }

        # ── OPERATION ORDER ────────────────────────────────
        # 1. Initial floors/ceilings already applied inside _build_container_resources.
        # 2. OOM bump if fresh event (computes desired lim/req; may be reverted
        #    by grow/shrink below — that's intentional, operator policy wins).
        # 3. Sticky OOM floor from prior bumps.
        # 4. grow/shrink vs apiserver-source old_res.
        # 5. Final floors/ceilings pass (hard invariants always win over policy).
        # 6. Detect "bump was reverted by policy": don't update oom_floor
        #    annotation, advance last_event for dedupe, log clear warning.
        # ─────────────────────────────────────────────────────────────────

        # Snapshot pre-bump for the suppression detector at the end.
        prev_lim_bytes_pre_bump = (
            _parse_memory_bytes(res["limits"]["memory"])
            if res.get("limits", {}).get("memory") else 0
        )

        # ── (2) OOM bump ──────────────────────────────────────────────────
        ev = events.get(c.container_name)
        oom_bump_attempted = False
        oom_bump_target_bytes = 0
        post_bump_lim_bytes = prev_lim_bytes_pre_bump
        if oom_eligible and ev is not None:
            stored = prior_last_event.get(c.container_name, "")
            if ev.finished_at > stored:
                # Runaway-bump cap (since 1.22.11). If this
                # container has already accumulated >=N bumps in history,
                # the workload has a structural memory bug (leak / GC /
                # sizing) that auto-bumping is masking. Stop bumping;
                # stamp investigation-required so operator sees it.
                # Still advance last_event so dedupe doesn't re-process
                # the same OOM every sync. Subsequent passes (sticky
                # floor, grow/shrink, final floors) run normally for the
                # container — only the bump-application is suppressed.
                prior_history_count = _count_history_entries(
                    prior_history.get(c.container_name, "")
                )
                bump_capped = prior_history_count >= _OOM_BUMPS_BEFORE_INVESTIGATION
                if bump_capped:
                    out_investigation[c.container_name] = True
                    out_last_event[c.container_name] = ev.finished_at
                    _log.warning(
                        "  [oom-bump-capped] %s/%s/%s: %d prior bumps in "
                        "history exceed cap of %d; stamping "
                        "oom-investigation-required (workload likely has a "
                        "structural memory bug — leak, GC misconfig, or "
                        "wrong sizing model — auto-bumping is masking it). "
                        "Clear via `oomFloorReset: \"true\"` after fixing.",
                        rec.namespace, rec.target_name, c.container_name,
                        prior_history_count, _OOM_BUMPS_BEFORE_INVESTIGATION,
                    )
                else:
                    oom_bump_attempted = True
                    oom_bump_target_bytes = int(ev.trap_limit_bytes * rc.oom_bump_factor)
                    # Cap at maxMemoryLimitMi (if set; 0 = unbounded). Operator
                    # ceiling beats bump — workload that exceeds wants explicit raise.
                    if rc.max_memory_limit_mi:
                        cap_bytes = rc.max_memory_limit_mi * (1 << 20)
                        if oom_bump_target_bytes > cap_bytes:
                            _log.warning(
                                "  [oom-bump-clamped] %s/%s/%s: bump wanted %d bytes "
                                "(%.0fMi) but maxMemoryLimitMi cap is %d Mi (%d bytes); "
                                "effective factor %.2f× instead of %.2f× — raise "
                                "maxMemoryLimitMi or lower oomBumpFactor to get the "
                                "full bump.",
                                rec.namespace, rec.target_name, c.container_name,
                                oom_bump_target_bytes,
                                oom_bump_target_bytes / (1 << 20),
                                rc.max_memory_limit_mi, cap_bytes,
                                cap_bytes / ev.trap_limit_bytes,
                                rc.oom_bump_factor,
                            )
                        oom_bump_target_bytes = min(oom_bump_target_bytes, cap_bytes)
                    res = _apply_oom_floor(res, oom_bump_target_bytes, rc.memory_limit_multiplier)
                    post_bump_lim_bytes = _parse_memory_bytes(res["limits"]["memory"])

        # ── (3) Sticky OOM floor from prior bumps ─────────────────────────
        if oom_floor_enabled:
            floor_bytes = prior_floor.get(c.container_name)
            if floor_bytes:
                res = _apply_oom_floor(res, floor_bytes, rc.memory_limit_multiplier)

        # ── (4) grow/shrink vs apiserver-source old_res ───────────────────
        # Operator policy — clamps res relative to what's currently in the
        # CR on the apiserver (source of truth for "what new pods will get").
        # Both flags true is a legitimate "freeze" configuration; the loop's
        # output ends up identical to old_res for all fields. Use case:
        # workload under change-freeze window where operator wants the tool
        # to keep managing admission (CR stays) but stop adjusting values.
        old_res = prev_res_lookup.get(c.container_name) if prev_res_lookup else None
        if (cfg.grow_only or cfg.shrink_only) and old_res:
            res_pre_clamp = res
            res = _apply_grow_shrink(
                res, old_res=old_res,
                grow_only=cfg.grow_only, shrink_only=cfg.shrink_only,
            )
            # Log when policy actually clamped a value (i.e. the new res
            # differs from the pre-clamp computation). Helps operator see
            # the cost of their flag.
            _log_grow_shrink_clamp(
                rec, c.container_name, res_pre_clamp, res, cfg.grow_only, cfg.shrink_only,
            )

        # ── (5) Final floors/ceilings pass ─────────────────────────────────
        # Hard invariants always win. If grow/shrink kept a value below the
        # floor or above the ceiling, this clamps it back into bounds.
        res = _enforce_floors(res, bounds)

        # ── (6) OOM bump suppression detection ────────────────────────────
        # Did the OOM bump's intended lim survive grow/shrink + final floors?
        # If not (typically because shrink_only reverted the growth), the
        # bump did NOT take effect on the actual CR limit. Don't record an
        # oom-floor annotation (truthful — floor reflects what was applied,
        # not what was attempted), don't add to history (same reason), but
        # DO advance last_event so dedupe doesn't re-process the same OOM
        # every sync.
        if oom_bump_attempted:
            final_lim_bytes = (
                _parse_memory_bytes(res["limits"]["memory"])
                if res.get("limits", {}).get("memory") else 0
            )
            out_last_event[c.container_name] = ev.finished_at
            bump_took_effect = final_lim_bytes >= post_bump_lim_bytes and final_lim_bytes > prev_lim_bytes_pre_bump

            if bump_took_effect:
                if oom_floor_enabled:
                    out_floor[c.container_name] = max(
                        prior_floor.get(c.container_name, 0), oom_bump_target_bytes,
                    )
                history_entry = _format_oom_boost_entry({
                    "from": _fmt_memory(str(prev_lim_bytes_pre_bump)) if prev_lim_bytes_pre_bump else "—",
                    "to": _fmt_memory(str(final_lim_bytes)),
                    "bump_factor": rc.oom_bump_factor,
                })
                out_history[c.container_name] = _build_oom_boost_history(
                    out_history.get(c.container_name, ""), history_entry,
                )
                _log.info(
                    "  [oom-bump] %s/%s/%s: %s → %s (trap=%s, ×%g)",
                    rec.namespace, rec.target_name, c.container_name,
                    _fmt_memory(str(prev_lim_bytes_pre_bump)) if prev_lim_bytes_pre_bump else "—",
                    _fmt_memory(str(final_lim_bytes)),
                    _fmt_memory(str(ev.trap_limit_bytes)),
                    rc.oom_bump_factor,
                )
            elif final_lim_bytes == prev_lim_bytes_pre_bump and post_bump_lim_bytes == prev_lim_bytes_pre_bump:
                # Bump was a no-op because floors/ceilings already covered it.
                # Same as 1.11.0 / 1.12.0 behavior.
                _log.info(
                    "  [oom-noop] %s/%s/%s: trap=%s × %g already covered by limit %s",
                    rec.namespace, rec.target_name, c.container_name,
                    _fmt_memory(str(ev.trap_limit_bytes)),
                    rc.oom_bump_factor,
                    _fmt_memory(str(final_lim_bytes)),
                )
            else:
                # Bump computed and lim raised at step (2), but a downstream
                # operation reverted it. Almost certainly grow/shrink policy
                # (shrink_only specifically). Log loudly so operator knows
                # the pod will continue OOMing and can change the policy.
                _log.warning(
                    "  [oom-bump-suppressed] %s/%s/%s: shrinkOnly clamped bump %s → %s back to %s; "
                    "pod will continue OOMing until operator removes shrinkOnly OR sets "
                    "oomDetectionEnabled=false on this workload.",
                    rec.namespace, rec.target_name, c.container_name,
                    _fmt_memory(str(prev_lim_bytes_pre_bump)) if prev_lim_bytes_pre_bump else "—",
                    _fmt_memory(str(post_bump_lim_bytes)),
                    _fmt_memory(str(final_lim_bytes)),
                )

        requests = res.get("requests") or {}
        limits = res.get("limits") or {}
        if not requests and not limits:
            continue

        entry: dict = {"name": c.container_name}
        if requests:
            entry["requests"] = dict(requests)
        if limits:
            entry["limits"] = dict(limits)
        payload.append(entry)

    # Build the per-container annotation map. Only emit annotations for
    # containers that have non-empty state. The rules are:
    #   - container is in the current workload spec (`rec.containers`)
    #     AND was rendered this sync → emit (normal path).
    #   - container is in the workload spec but skipped from rendering
    #     this sync (e.g. transient Prom miss + still in prior_floor)
    #     → emit (carry-forward, transient-miss safety).
    #   - container is NOT in the workload spec (removed from Deployment
    #     or renamed) → DROP, even if prior_floor still has an entry.
    #     The prior carry-forward was designed to survive Prom misses,
    #     not permanent container removal. Without this drop, an
    #     `oom-floor.<old-container>` annotation lingers forever
    #     after a rename — confuses `kubectl describe ro`.
    oom_annotations: dict[str, str] = {}
    rendered_names = {p["name"] for p in payload}
    current_container_names = {c.container_name for c in rec.containers}
    for container in sorted(out_floor):
        if container not in current_container_names:
            continue  # orphan from removed/renamed container
        if container not in rendered_names and container not in prior_floor:
            continue
        oom_annotations[floor_annotation_key(container)] = _fmt_memory(str(out_floor[container]))
    for container in sorted(out_last_event):
        if container not in current_container_names:
            continue
        if container not in rendered_names and container not in prior_last_event:
            continue
        oom_annotations[last_event_annotation_key(container)] = out_last_event[container]
    for container in sorted(out_history):
        if container not in current_container_names:
            continue
        if container not in rendered_names and container not in prior_history:
            continue
        oom_annotations[history_annotation_key(container)] = out_history[container]
    # Investigation flag. Same orphan-drop rule as the other
    # OOM annotations (skip containers that no longer exist; preserve flag
    # if it was already there from a prior sync).
    for container in sorted(out_investigation):
        if container not in current_container_names:
            continue
        if container not in rendered_names and container not in prior_investigation:
            continue
        oom_annotations[investigation_annotation_key(container)] = "true"

    return payload, oom_annotations


@dataclass
class OomEvent:
    """A detected OOMKilled event for a single container of a workload.

    Slow-path scans pod statuses at sync time and produces one of these
    per `(namespace, workload, container)` triple, using the MOST RECENT
    OOM seen across replicas. `trap_limit_bytes` is the value of
    `pod.spec.containers[c].resources.limits.memory` at OOM time —
    authoritative (kernel killed at this value).
    """
    namespace: str
    workload_name: str
    container: str
    finished_at: str        # RFC3339Z, used as the durable dedupe key
    trap_limit_bytes: int   # the limit that was killing the container


def detect_oom_events(
    core_v1_api,
    namespaces: list[str],
    workload_keys: set[tuple[str, str]],
) -> dict[tuple[str, str, str], OomEvent]:
    """List pods in opted-in namespaces, scan `lastState.terminated`,
    and return the most-recent OOM per `(ns, workload, container)`.

    `workload_keys` is `{(ns, target_name)}` from `list_workloads` —
    we only emit events for pods that resolve to a known opt-in
    workload (filters out pods owned by non-controller resources or
    workload kinds we don't manage).

    Pods are mapped to workload via ownerReferences (Deployment via
    ReplicaSet, StatefulSet directly, DaemonSet directly). Pods with
    no resolvable workload are skipped silently.
    """
    from kubernetes.client.rest import ApiException

    events: dict[tuple[str, str, str], OomEvent] = {}
    for ns in sorted(set(namespaces)):
        try:
            result = core_v1_api.list_namespaced_pod(ns)
        except ApiException as exc:
            _log.warning("[oom] failed to list pods in %s: %s — skipping ns", ns, exc)
            continue
        for pod in result.items or []:
            workload_name = _resolve_workload_name_from_pod(pod)
            if not workload_name:
                continue
            if (ns, workload_name) not in workload_keys:
                continue
            spec_limits = _spec_memory_limits(pod)
            for cs in pod.status.container_statuses or []:
                last_state = cs.last_state
                term = last_state.terminated if last_state else None
                if not term or term.reason != "OOMKilled":
                    continue
                finished_at = _normalize_rfc3339(term.finished_at)
                if not finished_at:
                    continue
                trap_limit = spec_limits.get(cs.name, 0)
                if not trap_limit:
                    continue
                key = (ns, workload_name, cs.name)
                prev = events.get(key)
                if prev is None or finished_at > prev.finished_at:
                    events[key] = OomEvent(
                        namespace=ns,
                        workload_name=workload_name,
                        container=cs.name,
                        finished_at=finished_at,
                        trap_limit_bytes=trap_limit,
                    )
    return events


def _resolve_workload_name_from_pod(pod) -> str:
    """Extract Deployment/StatefulSet/DaemonSet name from pod ownerRefs."""
    meta = pod.metadata
    refs = (meta.owner_references if meta else None) or []
    for ref in refs:
        kind = ref.kind or ""
        name = ref.name or ""
        if not name:
            continue
        if kind in ("StatefulSet", "DaemonSet"):
            return name
        if kind == "ReplicaSet":
            # Strip pod-template-hash suffix: "myapp-79c8d8d8f" → "myapp".
            parts = name.rsplit("-", 1)
            if len(parts) == 2 and parts[1] and parts[1].isalnum():
                return parts[0]
            return name
    return ""


def _spec_memory_limits(pod) -> dict[str, int]:
    """`{container_name: limit_bytes}` from pod.spec.containers."""
    out: dict[str, int] = {}
    spec = pod.spec
    for c in (spec.containers if spec else None) or []:
        resources = c.resources
        if resources is None:
            continue
        limits = resources.limits or {}
        mem = limits.get("memory")
        if not mem:
            continue
        try:
            out[c.name] = _parse_memory_bytes(mem)
        except (ValueError, TypeError):
            continue
    return out


def _normalize_rfc3339(value) -> str | None:
    """Coerce `finishedAt` (datetime or string) to RFC3339Z string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(value).strip()
    return s or None


def _apply_oom_floor(res: dict, floor_bytes: int, multiplier: float) -> dict:
    """Clamp memory request/limit up to the OOM floor.

    The floor is the highest limit the webhook fast-path has bumped this
    container to after observing an OOMKilled. Recommendation never
    drops below that — re-OOMing at a smaller limit defeats the bump.

    `multiplier` is the memory limit multiplier; request rises
    proportionally so `lim/req` stays within the chart's expected ratio
    (typically 3x).
    """
    new_res = dict(res)
    new_res["limits"] = dict(new_res.get("limits") or {})
    new_res["requests"] = dict(new_res.get("requests") or {})

    current_lim = (new_res["limits"].get("memory") or "")
    current_lim_bytes = _parse_memory_bytes(current_lim) if current_lim else 0
    if floor_bytes > current_lim_bytes:
        new_res["limits"]["memory"] = _fmt_memory(str(floor_bytes))

    # Keep request:limit ratio sane — scale request to floor / multiplier
    # if the multiplier is set, otherwise leave the request alone (the
    # caller's enforce_floors run already enforced the configured min).
    mult = multiplier or 1.0
    target_req_bytes = int(floor_bytes / mult)
    current_req = (new_res["requests"].get("memory") or "")
    current_req_bytes = _parse_memory_bytes(current_req) if current_req else 0
    if target_req_bytes > current_req_bytes:
        new_res["requests"]["memory"] = _fmt_memory(str(target_req_bytes))

    return new_res


def _delta(new_val: str | None, old_val: str | None, is_cpu: bool) -> str:
    """Return a `(+N%)` / `(-N%)` suffix for the new vs old quantity, empty when
    the change is below 1% or one side is missing.
    """
    if not new_val or not old_val or new_val == old_val:
        return ""
    parse = _parse_cpu_m if is_cpu else _parse_memory_bytes
    try:
        n = parse(new_val); o = parse(old_val)
    except (ValueError, TypeError):
        return ""
    if o == 0:
        return ""
    pct = (n - o) * 100 // o
    if pct == 0:
        return ""
    return f" ({'+' if pct > 0 else ''}{pct}%)"


def _value_with_delta(new_val: str | None, old_val: str | None, is_cpu: bool) -> str:
    """Render `<value>` or `<old> → <new> (+/-N%)` depending on whether the
    value changed materially since the previous CR.

    The before→after form makes the log self-contained: the operator no
    longer needs to dig into the previous MR to learn what the value was
    BEFORE the proposed change. Falls back to the bare new value when
    there's no previous (first sync) or the change is below the 1%
    threshold `_delta` already uses.
    """
    new_disp = new_val or "—"
    if not new_val or not old_val or new_val == old_val:
        return new_disp
    delta = _delta(new_val, old_val, is_cpu=is_cpu)
    if not delta:
        return new_disp
    return f"{old_val} → {new_val}{delta}"


def _log_entry_deltas(entries: list[WebhookEntry], old_docs: dict[str, dict], path: str) -> None:
    """One-line-per-container summary of the resources written into each CR.

    `old_docs` maps relative file path → {cr_name: parsed_doc}. With the
    one-file-per-Namespace layout, a single file holds many ResourceOverride
    documents; we look up each entry's previous values by its CR name within
    the file's index.
    """
    by_ns: dict[str, list[WebhookEntry]] = {}
    for e in entries:
        by_ns.setdefault(e.namespace, []).append(e)

    # Blank line BEFORE every [OK] namespace block (including the first) so
    # the write-phase visually separates from the discovery-phase preamble
    # printed by main.cmd_sync. Log viewers like Argo CD's strip leading
    # whitespace per line, so blank lines are the only reliable separator
    # between dense per-container delta blocks.
    for ns, ns_entries in by_ns.items():
        _log.info("")
        first = ns_entries[0]
        rel = _cr_relpath(first, path)
        _log.info("[OK] %s: %s", ns, rel,
                  extra={"namespace": ns, "cr_path": rel, "status": "ok"})
        # Width of the `cr_name/container` column across this namespace's
        # entries — padding to the widest entry's label makes the per-line
        # `req=`/`lim=` blocks line up vertically, much easier to scan than
        # ragged-right output. Containers are emitted as a tree under the
        # `[OK] <ns>:` header using `├─` / `└─` connectors so the
        # hierarchy survives log viewers that strip leading whitespace
        # (Argo CD UI strips spaces, preserves Unicode box-drawing chars).
        ns_containers: list[tuple[WebhookEntry, dict]] = [
            (e, c) for e in ns_entries for c in e.containers
        ]
        label_w = max(
            (len(f"{e.cr_name}/{c['name']}") for e, c in ns_containers),
            default=0,
        )
        old_for_file = old_docs.get(rel) or {}
        for idx, (e, c) in enumerate(ns_containers):
            connector = "└─" if idx == len(ns_containers) - 1 else "├─"
            old_doc = old_for_file.get(e.cr_name) if isinstance(old_for_file, dict) else None
            old_containers = _old_containers_by_name(old_doc or {})
            req_cpu = c.get("requests", {}).get("cpu")
            req_mem = c.get("requests", {}).get("memory")
            lim_cpu = c.get("limits",   {}).get("cpu")
            lim_mem = c.get("limits",   {}).get("memory")
            old = old_containers.get(c["name"], {})
            old_req = old.get("requests", {}) or {}
            old_lim = old.get("limits", {}) or {}
            label = f"{e.cr_name}/{c['name']}".ljust(label_w)
            # Render each value as either `<v>` or `<old> → <new> (+/-N%)`
            # so the log is self-contained — the operator sees what
            # changed AND what it changed FROM without opening the MR.
            req_cpu_s = _value_with_delta(req_cpu, old_req.get("cpu"),    is_cpu=True)
            req_mem_s = _value_with_delta(req_mem, old_req.get("memory"), is_cpu=False)
            lim_cpu_s = _value_with_delta(lim_cpu, old_lim.get("cpu"),    is_cpu=True)
            lim_mem_s = _value_with_delta(lim_mem, old_lim.get("memory"), is_cpu=False)
            # "Unchanged" = every value either has no prior baseline (—)
            # or the new value equals the old. The text formatter renders
            # the whole line gray so the scan jumps past it.
            unchanged = (
                req_cpu_s == (req_cpu or "—")
                and req_mem_s == (req_mem or "—")
                and lim_cpu_s == (lim_cpu or "—")
                and lim_mem_s == (lim_mem or "—")
            )
            extras = {
                "namespace": ns,
                "workload": e.cr_name,
                "container": c["name"],
                "req_cpu": req_cpu,
                "req_mem": req_mem,
                "lim_cpu": lim_cpu,
                "lim_mem": lim_mem,
            }
            if unchanged:
                extras["unchanged"] = True
            _log.info(
                "  %s %s  req=cpu:%s mem:%s  lim=cpu:%s mem:%s",
                connector, label,
                req_cpu_s, req_mem_s,
                lim_cpu_s, lim_mem_s,
                extra=extras,
            )


def _old_containers_by_name(doc: dict) -> dict[str, dict]:
    """Index a previously committed CR's containers by name for delta lookup."""
    out: dict[str, dict] = {}
    for c in (doc.get("spec") or {}).get("containers") or []:
        if isinstance(c, dict) and c.get("name"):
            out[c["name"]] = c
    return out


# Stable subset of the k8s.io recommended labels — the keys we read from a
# workload's PodTemplate to build a selector that's unique enough that the
# admission webhook never matches two CRs to the same pod. Skipped on
# purpose:
#   - `version`     — changes on every chart upgrade, would invalidate the CR.
#   - `managed-by`  — typically the constant "Helm", adds nothing.
#   - `part-of`     — chart-author discretion, not always present and not
#                     always distinguishing (Loki sets "memberlist" only on
#                     some workloads, none on others).
# Combined, instance + name + component covers Bitnami, kube-prometheus-stack,
# Loki, Argo CD, n8n, sonarqube, grafana, jaeger and friends — every chart we
# have audited so far. See the comment block at the top of the module for the
# survey methodology.
_SELECTOR_LABEL_KEYS = (
    "app.kubernetes.io/instance",
    "app.kubernetes.io/name",
    "app.kubernetes.io/component",
)


def _derive_selector(rec) -> dict[str, str]:
    """Pick the most distinguishing stable subset of labels from the workload's
    PodTemplate so the CR's matchLabels never overlaps with a sibling CR.

    Loki specifically motivated this: its chart deploys four StatefulSets that
    share `instance: loki` AND `name: loki`; only `component`
    (`single-binary` / `memcached-chunks-cache` / `memcached-results-cache` /
    `gateway`) tells them apart. Pre-1.4.1 the selector was just
    `instance: loki`, every Loki pod matched every loki-* CR, the webhook
    fell back to "last write wins" with a `[mutate] container memcached
    matched by multiple overrides` warning per admission.

    Fallback (workloads with NONE of the recommended labels — raw kustomize,
    plain manifests): `app.kubernetes.io/name: <workload-name>`. Same as
    pre-1.4.1 behaviour for that case.
    """
    selector = {}
    for k in _SELECTOR_LABEL_KEYS:
        v = rec.pod_template_labels.get(k) if hasattr(rec, "pod_template_labels") else None
        if v:
            selector[k] = v
    if selector:
        return selector
    # Legacy path: workload has no app.kubernetes.io/* labels we can use.
    if rec.helm_release:
        return {"app.kubernetes.io/instance": rec.helm_release}
    return {"app.kubernetes.io/name": rec.target_name}


def _cr_relpath(e: WebhookEntry, cr_path: str) -> str:
    """Path of the CR file relative to the cloned repo root.

    One file per Namespace directly under `cr_path`. Multiple workloads in
    the same Namespace share this file as separate YAML documents (see
    _render_namespace_file).
    """
    return f"{cr_path}/{e.namespace}{CR_FILE_SUFFIX}"


# --------------------------------------------------------------------------- #
# YAML emission                                                                #
# --------------------------------------------------------------------------- #

def _render_cr_doc(e: WebhookEntry, prev_doc: dict | None = None) -> dict:
    """Build the in-memory ResourceOverride document for a single workload.

    Used by `_render_namespace_file` to aggregate multiple workloads of the
    same Namespace into a multi-document YAML file.

    `prev_doc` is the previously committed CR for this workload (parsed
    from the existing namespace file). Carry-forward strategy:

      - **OOM annotations** (`oom-floor.<c>` / `oom-last-event.<c>` /
        `oom-boost-history.<c>`, plus the legacy single-key forms): the
        slow-path's bump logic owns these, sets them via
        `WebhookEntry.oom_annotations`. We do NOT carry forward from
        prev_doc here — the bump logic already merged with prev state
        before producing `oom_annotations`. Legacy single-key annotations
        on prev_doc are dropped (migration to per-container completes
        on this write).
      - **Other operator-added annotations** (cost-center, team labels,
        etc): carried forward verbatim. The slow-path is the CR's
        rebuilder, but it must not wipe operator metadata.
      - **Labels**: same — the tool's `app.kubernetes.io/managed-by` is
        set unconditionally, plus everything else from prev_doc.
    """
    annotations: dict[str, str] = {}
    labels: dict[str, str] = {}
    if prev_doc:
        prev_meta = prev_doc.get("metadata") or {}
        for k, v in (prev_meta.get("annotations") or {}).items():
            if _is_oom_annotation(k):
                # Owned by the OOM logic — handled below via
                # e.oom_annotations. Skip here to drop legacy CSV keys
                # cleanly (the new per-container shape replaces them).
                continue
            annotations[k] = v
        labels.update(prev_meta.get("labels") or {})

    # OOM annotations come from the entry — bump logic merged with
    # prev state already, so this is the canonical post-merge view.
    if e.oom_annotations:
        annotations.update(e.oom_annotations)

    # Tool-owned label always present.
    labels["app.kubernetes.io/managed-by"] = "kube-resource-updater"

    metadata: dict = {
        "name": e.cr_name,
        "namespace": e.namespace,
        "labels": labels,
    }
    if annotations:
        metadata["annotations"] = annotations

    # Force-quote every selector label value so ruamel emits them with
    # explicit quotes. Without this, label values that LOOK like YAML 1.1
    # booleans (`on`, `off`, `yes`, `no`) get emitted unquoted, and
    # downstream Go yaml.v2 parsers (ArgoCD, kubectl convert paths, the
    # apiserver's manifest reader) coerce them to `true`/`false` instead
    # of strings — apiserver then rejects the CR because matchLabels
    # values MUST be strings.
    match_labels = {
        k: DoubleQuotedScalarString(v) for k, v in e.selector_labels.items()
    }

    return {
        "apiVersion": CR_API_VERSION,
        "kind": CR_KIND,
        "metadata": metadata,
        "spec": {
            "selector": {
                "matchLabels": match_labels,
            },
            "containers": [_ordered_container(c) for c in e.containers],
        },
    }


def _render_namespace_file(
    entries_for_ns: list[WebhookEntry],
    prev_docs: dict[str, dict] | None = None,
    preserve_cr_names: set[str] | None = None,
) -> str:
    """Render all ResourceOverrides for one Namespace into a single file.

    Stable ordering: workloads sorted by their CR name so diffs across runs only
    reflect actual value changes. Multi-document YAML (`---` separator) per
    workload — kubectl, kubeval and ArgoCD all consume this format natively.

    `prev_docs` maps CR name → previously committed CR dict for this file,
    used to carry forward the `oom-boost-history` annotation across syncs.

    `preserve_cr_names` lists CR names whose previously-committed content should
    be re-emitted **verbatim** from `prev_docs` instead of being rendered from a
    new entry. Used during pass-1 of bucket-split writes: workloads in the OTHER
    bucket (e.g. MR bucket during a direct-push pass) carry forward unchanged so
    the on-disk file doesn't accidentally drop them. Workloads listed in both
    `entries_for_ns` AND `preserve_cr_names` are treated as new (the entry
    wins — defensive against caller confusion).
    """
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.preserve_quotes = True
    yaml.explicit_start = True   # emit leading '---' on every document

    prev_docs = prev_docs or {}
    preserve = set(preserve_cr_names or ())
    rendered: list[tuple[str, dict]] = []

    # New entries: rendered through the normal _render_cr_doc path so they pick
    # up prev_doc carry-forward (non-OOM annotations, labels) automatically.
    new_names: set[str] = set()
    for e in entries_for_ns:
        new_names.add(e.cr_name)
        rendered.append((e.cr_name, _render_cr_doc(e, prev_docs.get(e.cr_name))))

    # Preserve entries: emit prev_doc directly. Skips anything that's also being
    # re-rendered as a new entry (new wins).
    for name in preserve - new_names:
        prev = prev_docs.get(name)
        if prev is not None:
            rendered.append((name, prev))

    rendered.sort(key=lambda kv: kv[0])
    buf = StringIO()
    for _, doc in rendered:
        yaml.dump(doc, buf)
    return buf.getvalue()


def _ordered_container(c: dict) -> dict:
    """Re-emit a container entry in the canonical order (name first, then requests, then limits)."""
    out = {"name": c["name"]}
    if c.get("requests"):
        out["requests"] = c["requests"]
    if c.get("limits"):
        out["limits"] = c["limits"]
    return out


# --------------------------------------------------------------------------- #
# Git commit / push / MR                                                       #
# --------------------------------------------------------------------------- #

def _commit_repo(
    repo_url: str,
    branch: str,
    path: str,
    entries: list[WebhookEntry],
    provider: GitProvider,
    git_author_name: str,
    git_author_email: str,
    mr_config: MrConfig | None = None,
    auto_rollout_by_namespace: "dict[str, bool] | None" = None,
) -> list[tuple[str, list[str]]]:
    """Clone the repo, bucket-split entries by per-workload `create_mr`, push.

    Returns a list of (result_url, [namespace, ...]) — at most two entries:
    one for the direct push (when any entry has `create_mr=False`) and one
    for the MR (when any entry has `create_mr=True`). Empty list when nothing
    changed for either bucket.

    Bucketing details: the hierarchy helm < ns < workload is
    resolved per entry by the caller (`_build_entries`), so this function
    just trusts `entry.create_mr`. The two passes share a single clone and a
    single `_read_old_docs` snapshot — the second pass uses `git pull` to
    pick up the first pass's commit on the target branch before opening the
    MR branch, so the MR diff only shows the second bucket's changes.

    Orphan cleanup (files for namespaces that no longer have ANY entry) runs
    in the LAST pass that actually executes — direct-only or MR-only does it
    in its own pass, mixed runs do it in pass-2 (MR).
    """
    direct_entries = [e for e in entries if not e.create_mr]
    mr_entries = [e for e in entries if e.create_mr]

    # Per-workload createMr annotation can flip individual workloads to
    # MR-mode even when the helm-level `cfg.create_mr` is false. The
    # startup `Config.validate` only checks the helm-level toggle ×
    # token, so this combination escaped: helm false + token empty +
    # workload annotation `createMr=true`. The sync would clone the
    # repo, build the diff, and crash mid-flight with a 401 from the
    # GitLab MR-open call. Failing here — before any git I/O — gives
    # the operator a single clear error and preserves the same exit
    # code as the validate.yaml render-time check.
    if mr_entries and not provider.has_credentials():
        ns_list = sorted({e.namespace for e in mr_entries})
        wl_list = sorted({f"{e.namespace}/{e.cr_name}" for e in mr_entries})
        _log.error(
            "[mr] %d workload(s) annotated createMr=true but no git token is "
            "configured — the MR-open call would crash with 401 mid-sync. Affected: "
            "%s. Either configure git credentials in chart "
            "values, or remove the per-workload createMr=true annotations "
            "(workloads: %s) to fall back to direct push.",
            len(mr_entries),
            ", ".join(ns_list),
            ", ".join(wl_list[:5]) + (f", +{len(wl_list)-5} more" if len(wl_list) > 5 else ""),
        )
        return []

    auth_url = provider.auth_url(repo_url)
    results: list[tuple[str, list[str]]] = []

    with tempfile.TemporaryDirectory(prefix="aru-webhook-") as tmp:
        repo_dir = os.path.join(tmp, "repo")
        _run(["git", "clone", "--depth", "1", "--branch", branch, auth_url, repo_dir])

        # ONE read of the prior state, used by both passes. The same snapshot
        # also feeds the MR description and log deltas — those report against
        # the state before THIS sync started, regardless of bucket order.
        old_docs = _read_old_docs(repo_dir, entries, path)

        _run(["git", "-C", repo_dir, "config", "user.name",  git_author_name])
        _run(["git", "-C", repo_dir, "config", "user.email", git_author_email])

        # ── Pass 1: direct push ────────────────────────────────────────
        # Writes the file with direct-bucket entries (new content) PLUS
        # carry-forward for any MR-bucket workloads in the same file
        # (so we don't accidentally drop them). Orphan cleanup deferred
        # to pass-2 unless pass-2 won't run.
        direct_pushed = False
        if direct_entries:
            changed = _write_namespace_files(
                repo_dir,
                entries=direct_entries,
                path=path,
                old_docs=old_docs,
                preserve_entries=mr_entries,
            )
            deleted = _prune_orphan_files(repo_dir, entries, path) if not mr_entries else []
            _log_entry_deltas(direct_entries, old_docs, path)

            if changed or deleted:
                if changed:
                    _run(["git", "-C", repo_dir, "add", "--", *changed])
                if deleted:
                    _run(["git", "-C", repo_dir, "rm", "--quiet", "--", *deleted])

                if _run(["git", "-C", repo_dir, "diff", "--cached", "--quiet"], allow_nonzero=True).returncode:
                    commit_msg = _commit_message(direct_entries, deleted)
                    _run(["git", "-C", repo_dir, "commit", "-m", commit_msg])
                    _run(["git", "-C", repo_dir, "push", "origin", branch])
                    direct_pushed = True
                    ns_list = sorted({e.namespace for e in direct_entries})
                    # Blank between the per-namespace trees and this
                    # commit-summary line (mirrors the MR path below).
                    _log.info("")
                    # `[push]` tag — was `[OK] webhook: ...`
                    # before; semantically `[OK]` belongs to per-namespace
                    # CR-file ACK, while this line reports the per-repo
                    # commit result. `[push]` mirrors `[mr]` on the
                    # MR-bucket path so the two outcomes look symmetric.
                    _log.info(
                        "[push] %d file(s), %d orphan(s) removed (%s@%s)",
                        len(changed), len(deleted), repo_url, branch,
                        extra={
                            "files_changed": len(changed),
                            "files_deleted": len(deleted),
                            "mode": "direct",
                            "namespaces": ns_list,
                            "repo_url": repo_url,
                            "branch": branch,
                        },
                    )
                    results.append((f"{repo_url}@{branch}", ns_list))
            else:
                _log.info("[OK] webhook: no direct-push CR changes for %s", repo_url)

        # ── Pass 2: MR branch ──────────────────────────────────────────
        # Forks from the up-to-date target branch (post-direct-push if pass 1
        # ran). Writes ALL entries — direct-bucket files now match what's on
        # main from pass 1 so they produce zero diff; MR-bucket files differ
        # and show up in the MR.
        if mr_entries:
            if direct_pushed:
                # Refresh our working copy so the MR branch forks from the
                # updated tip of `branch`, not the pre-push HEAD.
                _run(["git", "-C", repo_dir, "fetch", "origin", branch])
            work_branch = "resource-updater/sync"
            _run(["git", "-C", repo_dir, "checkout", "-B", work_branch, f"origin/{branch}"])

            changed = _write_namespace_files(
                repo_dir,
                entries=entries,
                path=path,
                old_docs=old_docs,
            )
            deleted = _prune_orphan_files(repo_dir, entries, path)
            _log_entry_deltas(mr_entries, old_docs, path)

            if changed or deleted:
                if changed:
                    _run(["git", "-C", repo_dir, "add", "--", *changed])
                if deleted:
                    _run(["git", "-C", repo_dir, "rm", "--quiet", "--", *deleted])

                if _run(["git", "-C", repo_dir, "diff", "--cached", "--quiet"], allow_nonzero=True).returncode:
                    commit_msg = _commit_message(mr_entries, deleted)
                    _run(["git", "-C", repo_dir, "commit", "-m", commit_msg])
                    _run(["git", "-c", "push.useForceWithLease=false",
                          "-C", repo_dir, "push", "--force",
                          "--set-upstream", "origin", work_branch])

                    assignee_ids = provider.resolve_users(mr_config.assignees if mr_config else [])
                    reviewer_ids = provider.resolve_users(mr_config.reviewers if mr_config else [])
                    # Blank line between the per-namespace `[OK] ns:` tree
                    # blocks (last log call before this point) and the
                    # commit-summary lines ([mr] metadata + [OK] webhook:
                    # MR opened). Without this they ran into the last
                    # container tree line, hard to scan visually.
                    _log.info("")
                    if mr_config and (assignee_ids or reviewer_ids or mr_config.labels):
                        _log.info(
                            "[mr] metadata: assignees=%s reviewers=%s labels=%s squash=%s",
                            mr_config.assignees or "—",
                            mr_config.reviewers or "—",
                            mr_config.labels or "—",
                            mr_config.squash,
                            extra={
                                "assignees": mr_config.assignees,
                                "reviewers": mr_config.reviewers,
                                "labels": mr_config.labels,
                                "squash": mr_config.squash,
                            },
                        )
                    mr_url = provider.open_or_update_pr(
                        source_branch=work_branch,
                        target_branch=branch,
                        title=_mr_title(mr_entries),
                        description=_mr_description(mr_entries, old_docs, path,
                                                     auto_rollout_by_namespace=auto_rollout_by_namespace),
                        assignees=assignee_ids,
                        reviewers=reviewer_ids,
                        labels=(mr_config.labels if mr_config else None),
                        squash=(mr_config.squash if mr_config else False),
                        remove_source_branch=(mr_config.remove_source_branch if mr_config else True),
                        project_path=_project_path_from_url(repo_url),
                    )
                    ns_list = sorted({e.namespace for e in mr_entries})
                    # `[mr] opened` — was `[OK] webhook: MR
                    # opened ...` before. `[OK]` is reserved for per-ns CR
                    # file ACK; this line is the per-repo MR result and
                    # belongs to the same `[mr]` family as the metadata
                    # line emitted just above.
                    _log.info("[mr] opened %s (branch %s → %s)",
                              mr_url, work_branch, branch,
                              extra={
                                  "mr_url": mr_url,
                                  "source_branch": work_branch,
                                  "target_branch": branch,
                                  "namespaces": ns_list,
                                  "mode": "mr",
                              })
                    results.append((mr_url, ns_list))
            else:
                _log.info("[OK] webhook: no MR CR changes for %s", repo_url)

        if not results:
            _log.info("[OK] webhook: no CR changes for %s", repo_url)

    return results


def _read_old_docs(repo_dir: str, entries: list[WebhookEntry], path: str) -> dict[str, dict]:
    """Parse the previously committed multi-doc CR file for each Namespace.

    Returns: relpath → list of CR dicts (one per workload). Each file holds many
    YAML documents; we index by relpath so callers can look up the file content
    by `_cr_relpath(entry, path)` and then extract the workload they care about
    by `metadata.name`.
    """
    out: dict[str, dict] = {}
    yaml = YAML()
    # Mirror the renderer's preserve_quotes so values round-trip with the
    # same quoting they were emitted with. Without this, selector label
    # values like `"yes"` get loaded as plain str, and the preserve-only
    # re-emit path in _render_namespace_file drops the quotes — producing
    # a spurious diff against the file _render_cr_doc just wrote in pass-1.
    yaml.preserve_quotes = True
    seen: set[str] = set()
    for e in entries:
        rel = _cr_relpath(e, path)
        if rel in seen:
            continue
        seen.add(rel)
        abs_path = os.path.join(repo_dir, rel)
        if not os.path.exists(abs_path):
            continue
        try:
            with open(abs_path, encoding="utf-8") as fh:
                docs = list(yaml.load_all(fh))
            by_cr_name = {}
            for d in docs:
                if isinstance(d, dict) and (d.get("metadata") or {}).get("name"):
                    by_cr_name[d["metadata"]["name"]] = d
            out[rel] = by_cr_name
        except (YAMLError, OSError) as exc:
            # Malformed YAML in the gitops repo OR disappearing file between
            # exists() and open(). Skip this file's prev_doc — the new write
            # will overwrite it from a clean state. Wider exceptions
            # (AttributeError on an unexpected doc shape, etc) indicate a
            # bug; let them surface.
            _log.warning("[webhook] could not read prior CR file %s: %s", rel, exc)
            continue
    return out


def _write_namespace_files(
    repo_dir: str,
    entries: list[WebhookEntry],
    path: str,
    old_docs: dict[str, dict] | None = None,
    preserve_entries: list[WebhookEntry] | None = None,
) -> list[str]:
    """Write one multi-doc YAML file per Namespace and return changed relpaths.

    Files whose content matches what was already on disk are not re-written, so
    `git add` only stages real changes.

    `old_docs` (relpath → {cr_name: prev_doc_dict}) feeds prior CR documents
    into the renderer so annotations like `oom-boost-history` survive across
    syncs.

    `preserve_entries` is the bucket-split companion: workloads whose CR
    document should be re-emitted from `old_docs` as-is (instead of from a
    fresh entry). The caller passes here the OTHER bucket's entries during
    each pass so the file on disk retains them while the current bucket
    writes its new content. Without this, pass-1 would write a file
    containing only its bucket's workloads and ArgoCD would prune the rest.
    """
    by_ns: dict[tuple[str, str], list[WebhookEntry]] = defaultdict(list)
    for e in entries:
        by_ns[(e.namespace, _cr_relpath(e, path))].append(e)

    # Group preserve entries by the same (ns, relpath) key so they can be
    # passed alongside the new entries for the matching file. Preserve entries
    # whose file ALSO has new entries get bundled; preserve entries whose file
    # has no new entries this pass need their own file write below.
    preserve_by_ns: dict[tuple[str, str], list[WebhookEntry]] = defaultdict(list)
    for e in preserve_entries or ():
        preserve_by_ns[(e.namespace, _cr_relpath(e, path))].append(e)

    old_docs = old_docs or {}
    changed: list[str] = []
    seen_files: set[tuple[str, str]] = set()

    for key, ns_entries in by_ns.items():
        _ns, rel = key
        seen_files.add(key)
        preserve_names = {e.cr_name for e in preserve_by_ns.get(key, [])}
        abs_path = os.path.join(repo_dir, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        new_content = _render_namespace_file(
            ns_entries,
            prev_docs=old_docs.get(rel),
            preserve_cr_names=preserve_names,
        )

        old_content = ""
        if os.path.exists(abs_path):
            with open(abs_path, encoding="utf-8") as fh:
                old_content = fh.read()
        if new_content == old_content:
            continue
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        changed.append(rel)

    # Files with ONLY preserve entries this pass (no new content for this
    # namespace's file). Re-emit the prev_doc so the file retains the
    # workloads from the other bucket. Critical for namespaces where pass-1
    # has zero new content but pass-2 will land changes — the file must not
    # be deleted (orphan cleanup runs separately and would skip it because
    # it's a known namespace).
    for key, ns_preserve in preserve_by_ns.items():
        if key in seen_files:
            continue
        _ns, rel = key
        preserve_names = {e.cr_name for e in ns_preserve}
        abs_path = os.path.join(repo_dir, rel)
        if not os.path.exists(abs_path):
            # No prior file → no preserve possible. Skip; the other bucket
            # will create the file in its own pass.
            continue
        new_content = _render_namespace_file(
            [],
            prev_docs=old_docs.get(rel),
            preserve_cr_names=preserve_names,
        )
        with open(abs_path, encoding="utf-8") as fh:
            old_content = fh.read()
        if new_content != old_content:
            with open(abs_path, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            changed.append(rel)

    return changed


def _prune_orphan_files(repo_dir: str, entries: list[WebhookEntry], path: str) -> list[str]:
    """Delete any tool-managed CR file the current run did not write to.

    Two scenarios this covers:

    1. Namespace stopped opting in. Its file lingers in git with stale CRs;
       deleting it during the next sync MR keeps the gitops repo in lock-step
       with cluster intent.
    2. Pre-refactor file names. The previous layouts wrote per-Application
       (`<app-name>.resource-override.yaml`) or per-workload
       (`<ns>/<workload>.yaml`) — anything that isn't `<ns>.resource-override.yaml`
       for a currently-opted-in namespace gets pruned.

    A file is considered tool-managed when its first document has
    `metadata.labels[app.kubernetes.io/managed-by] == kube-resource-updater`.
    Static manifests at the path root (e.g. dockerhub-pull-secret.yaml) are
    skipped because they don't carry that label.
    """
    if not path:
        return []
    base = os.path.join(repo_dir, path)
    if not os.path.isdir(base):
        return []

    expected_files: set[str] = {_cr_relpath(e, path) for e in entries}
    yaml = YAML()
    pruned: list[str] = []

    def _is_tool_owned(abs_path: str) -> bool:
        try:
            with open(abs_path, encoding="utf-8") as fh:
                docs = list(yaml.load_all(fh))
        except (YAMLError, OSError):
            # Unparseable or vanished mid-walk — definitely don't claim ownership.
            return False
        for d in docs:
            if not isinstance(d, dict):
                continue
            md = d.get("metadata") or {}
            labels = md.get("labels") or {}
            if labels.get("app.kubernetes.io/managed-by") == "kube-resource-updater":
                return True
        return False

    # Walk both the path root (current + per-Application legacy) and any
    # per-namespace subfolders (older legacy layout).
    for entry in sorted(os.listdir(base)):
        full = os.path.join(base, entry)
        if os.path.isdir(full):
            for fname in sorted(os.listdir(full)):
                if not fname.endswith(".yaml"):
                    continue
                abs_path = os.path.join(full, fname)
                if not _is_tool_owned(abs_path):
                    continue
                pruned.append(os.path.join(path, entry, fname))
            continue
        if not entry.endswith(CR_FILE_SUFFIX):
            continue
        rel = os.path.join(path, entry)
        if rel in expected_files:
            continue
        if _is_tool_owned(full):
            pruned.append(rel)
    return pruned


def _commit_message(entries: list[WebhookEntry], deleted_paths: list[str]) -> str:
    """One-line subject + summary of what changed."""
    n_ns = len({e.namespace for e in entries})
    n_workloads = len(entries)
    subject = f"chore(resources): update {n_workloads} ResourceOverride(s) across {n_ns} namespace(s)"
    lines = [subject, ""]
    if deleted_paths:
        lines.append(f"Pruned {len(deleted_paths)} orphan file(s) from previous layouts /"
                     " namespaces that no longer opt in.")
        lines.append("")
    lines.append("Generated by kube-resource-updater (webhook write-back mode).")
    return "\n".join(lines)


def _mr_title(entries: list[WebhookEntry]) -> str:
    n_ns = len({e.namespace for e in entries})
    n_workloads = len(entries)
    return f"chore(resources): update {n_workloads} ResourceOverride(s) across {n_ns} namespace(s)"


def _mr_description(
    entries: list[WebhookEntry],
    old_docs: dict[str, dict],
    path: str,
    auto_rollout_by_namespace: "dict[str, bool] | None" = None,
) -> str:
    """Markdown summary: per-container row with CPU/mem requests and limits,
    each annotated with `(+/-N%)` deltas vs the previously committed CR.
    """
    rows: list[str] = []
    files: list[str] = []
    seen_files: set[str] = set()
    for e in sorted(entries, key=lambda x: (x.namespace, x.cr_name)):
        rel = _cr_relpath(e, path)
        if rel not in seen_files:
            files.append(f"`{rel}`")
            seen_files.add(rel)
        old_for_file = old_docs.get(rel) or {}
        old_doc = old_for_file.get(e.cr_name) if isinstance(old_for_file, dict) else None
        old_containers = _old_containers_by_name(old_doc or {})
        for c in e.containers:
            old = old_containers.get(c["name"], {})
            old_req = (old.get("requests") or {})
            old_lim = (old.get("limits") or {})
            new_cpu_req = (c.get("requests") or {}).get("cpu")
            new_mem_req = (c.get("requests") or {}).get("memory")
            new_cpu_lim = (c.get("limits") or {}).get("cpu")
            new_mem_lim = (c.get("limits") or {}).get("memory")
            cpu_req = _fmt_delta_cpu(new_cpu_req, old_req.get("cpu"), emoji=True) if new_cpu_req else "n/a"
            mem_req = _fmt_delta_mem(new_mem_req, old_req.get("memory"), emoji=True) if new_mem_req else "n/a"
            cpu_lim = _fmt_delta_cpu(new_cpu_lim, old_lim.get("cpu"), emoji=True) if new_cpu_lim else "n/a"
            mem_lim = _fmt_delta_mem(new_mem_lim, old_lim.get("memory"), emoji=True) if new_mem_lim else "n/a"
            rows.append(
                f"| `{e.namespace}` | `{e.cr_name}` "
                f"| `{c['name']}` | {cpu_req} | {mem_req} | {cpu_lim} | {mem_lim} |"
            )

    files_str = "\n".join(f"- {f}" for f in files)

    # Footer: group namespaces by their effective autoRollout state.
    # `auto_rollout_by_namespace` is computed at sync time by `cmd_sync`
    # using `is_auto_rollout_enabled(False, ns_annotations, {})` — workload-
    # level annotation overrides are not reflected (would require per-entry
    # resolution); the namespace-level signal is the common case and matches
    # how operators usually configure rollout. Per-workload annotation
    # overrides still take effect at admission time; the footer just
    # summarises the most operator-visible level.
    namespaces_in_mr = sorted({e.namespace for e in entries})
    auto_rollout = auto_rollout_by_namespace or {}
    rolling = [ns for ns in namespaces_in_mr if auto_rollout.get(ns, False)]
    static = [ns for ns in namespaces_in_mr if not auto_rollout.get(ns, False)]

    footer_parts: list[str] = [
        "These CRs are read by the `kube-resource-updater-webhook` admission webhook.",
        "",
    ]
    if rolling:
        rolling_str = ", ".join(f"`{ns}`" for ns in rolling)
        footer_parts.append(
            f"- {rolling_str} — **autoRollout enabled**: changes take effect "
            f"within ~30s of merge (webhook stamps `restartedAt` on the "
            f"PodTemplate; kubelet rolls pods respecting `maxUnavailable` / "
            f"PDB)."
        )
    if static:
        static_str = ", ".join(f"`{ns}`" for ns in static)
        footer_parts.append(
            f"- {static_str} — changes take effect on the next pod admission "
            f"(deploy / restart / scale event). To enable automatic rollout, "
            f"annotate the namespace with "
            f"`kube-resource-updater.autoRollout: \"true\"`."
        )

    footer = "\n".join(footer_parts)
    body = (
        "## ResourceOverride CRs\n\n"
        "| Namespace | Workload | Container | CPU req | Mem req | CPU limit | Mem limit |\n"
        "|---|---|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n\n"
        f"**Modified files:**\n{files_str}\n\n"
        f"{footer}\n\n"
        "---\n*Auto-generated by **kube-resource-updater** (webhook write-back mode).*\n"
    )
    return _truncate_mr_description(body)


def _truncate_mr_description(body: str, cap_bytes: int = _MR_DESCRIPTION_CAP_BYTES) -> str:
    """Cap MR descriptions below the provider's hard byte limit.

    GitLab API returns 422 mid-sync when a description exceeds 1 MiB — the
    commit has already been pushed, but `requests.post(/merge_requests)`
    rejects. The operator sees a half-done sync and the workload owner
    never gets a review hand-off. We measure in BYTES (UTF-8 encoded), not
    `len()` characters: a description full of `→` arrows or non-ASCII
    namespace names can balloon past the cap while `len()` looks safe.

    Since 1.22.11: when truncation triggers, the footer reports
    how many container rows were dropped so the operator opening the MR
    doesn't assume the visible set is complete. Row counting is purely
    textual: lines starting with `| \\`` followed by enough `|` to be a
    container-row in the standard 7-column table layout that
    `_mr_description` emits.
    """
    encoded = body.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return body
    # Decode the truncation prefix with `errors="ignore"` so we never split
    # a multi-byte codepoint in half (would emit an invalid UTF-8 sequence
    # that GitLab's parser may reject). The truncate-to budget is cap-relative
    # so providers with a small cap (e.g. GitHub at 60 kB) still produce a
    # non-empty body after truncation — see _MR_DESCRIPTION_HEADROOM_BYTES.
    headroom = min(_MR_DESCRIPTION_HEADROOM_BYTES, cap_bytes // 4)
    truncate_to = max(cap_bytes - headroom, 0)
    truncated = encoded[:truncate_to].decode("utf-8", errors="ignore")
    rows_dropped = _count_container_rows(body) - _count_container_rows(truncated)
    return truncated + _MR_DESCRIPTION_TRUNCATION_FOOTER_FMT.format(
        n=max(rows_dropped, 0)
    )


def _count_container_rows(body: str) -> int:
    """Count container-rows in an MR description body.

    A container-row starts with `| \\`` (the namespace cell) and contains
    enough pipe characters to form the 7-column table layout that
    `_mr_description` emits (`| ns | wl | container | cpu_req | mem_req |
    cpu_lim | mem_lim |`). Header / separator rows don't start with
    backticked content. Robust to truncation that may have cut the last
    row mid-line — that line gets counted only if it still has >=7 pipes.
    """
    n = 0
    for line in body.split("\n"):
        if line.startswith("| `") and line.count("|") >= 7:
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Subprocess helper                                                            #
# --------------------------------------------------------------------------- #

# Default subprocess timeout for git calls. A degraded GitLab
# remote can hang `git clone` / `fetch` / `push` until the CronJob's
# activeDeadlineSeconds (default 1800s) hard-kills the pod, masking the
# failure as "slow run". 300s covers the 99th-percentile clone+push of
# the gitops repo (a few KB of YAML on `--depth 1`) with generous
# headroom; callers can override per-call when a specific git operation
# is expected to take longer.
_GIT_SUBPROCESS_TIMEOUT_S = 300


def _run(cmd: list[str], allow_nonzero: bool = False,
         timeout: float = _GIT_SUBPROCESS_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run a subprocess, raising on non-zero unless allow_nonzero is set."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        sanitized_cmd = [c if "@" not in c or "://" not in c else _strip_auth(c) for c in cmd]
        _log.error("git command timed out after %ss: %s",
                   timeout, " ".join(sanitized_cmd))
        raise
    if result.returncode != 0 and not allow_nonzero:
        sanitized_cmd = [c if "@" not in c or "://" not in c else _strip_auth(c) for c in cmd]
        # stdout/stderr are scrubbed too: git fills stderr with the clone URL
        # (including the token) on a failed clone, and `_strip_auth` is for a
        # single URL — a multi-line blob needs the regex redactor.
        _log.error("git command failed: %s\nstdout: %s\nstderr: %s",
                   " ".join(sanitized_cmd), _redact_auth(result.stdout), _redact_auth(result.stderr))
        try:
            result.check_returncode()
        except subprocess.CalledProcessError as exc:
            # check_returncode() sets exc.cmd = the original argv, which carries
            # the auth URL (token). Python prints exc.cmd in the default
            # traceback, so an unhandled exception would leak the token even
            # though the log above is scrubbed. Strip it before re-raising.
            exc.cmd = [c if "@" not in c or "://" not in c else _strip_auth(c)
                       for c in (exc.cmd or [])]
            raise
    return result


# Redact `<scheme>://user:token@` credentials embedded anywhere in arbitrary
# text (e.g. git's multi-line error output) before logging.
_AUTH_IN_TEXT_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+@")


def _redact_auth(text: str) -> str:
    """Scrub `user:token@` from every URL inside an arbitrary text blob.

    Unlike `_strip_auth` (which assumes the whole string is one URL), this
    handles multi-line output that merely *contains* credential-bearing URLs.
    """
    return _AUTH_IN_TEXT_RE.sub(r"\1***@", text or "")


def _strip_auth(url: str) -> str:
    """Remove user:token from a single URL before logging."""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    return f"{scheme}://{rest}"
