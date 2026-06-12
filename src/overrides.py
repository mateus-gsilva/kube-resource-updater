"""
Annotation-driven config overrides.

The tool resolves its effective config for a workload through a 3-level
hierarchy:

    Helm values  (cluster defaults from the ConfigMap)
        ↓ overridden by
    Namespace annotations  (`kube-resource-updater.<key>` on the Namespace)
        ↓ overridden by
    Workload annotations   (`kube-resource-updater.<key>` on the Deployment / StatefulSet)

All overrides use the same prefix (`kube-resource-updater.`) and the same
camelCase key names that appear in `values.yaml`. Anything not in
`KNOWN_KEYS` is logged as a typo warning so a misspelled annotation does not
silently fall back to the chart default.

Why annotations and not labels: label values are constrained (regex
`(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?`, max 63 chars) and labels are
indexed by the apiserver — they're meant for selectors, not configuration.
The webhook's namespace filtering happens in code (cached Namespace
informer) so it does not need a `LabelSelector`-friendly opt-in. See
ROADMAP "Annotation-only standardization" for the full rationale.

This module never reads from k8s; it only transforms dicts. The caller is
responsible for fetching `Namespace.metadata.annotations` and
`workload.metadata.annotations` and passing them in.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any
from collections.abc import Mapping

from src.config import Config

_log = logging.getLogger(__name__)

# Every annotation the tool reads — and the ONLY ones it reads — start with
# this prefix. Keys outside the prefix are ignored entirely (no warning).
ANNOTATION_PREFIX = "kube-resource-updater."

# Opt-in marker. Only valid on a Namespace; ignored on workloads.
OPT_IN_KEY = "enabled"

# Per-pod escape hatch read by the admission webhook to short-circuit before
# any patch is computed. Only valid on a workload (Deployment, StatefulSet,
# Pod); ignored on a Namespace.
SKIP_KEY = "skip"

# Auto-rollout opt-in/out — read by `src/webhook_rollout.py` after a
# ResourceOverride is created or updated. Resolution order is the same
# workload > namespace > helm-default chain. Stored as a marker key (not a
# Config field) because the webhook decides whether to act per CR event,
# not per sync-time Config object.
AUTO_ROLLOUT_KEY = "autoRollout"

# Comma-separated list of container names the sync loop must NOT include
# in the generated ResourceOverride CR. The webhook never patches a
# container that isn't named in any CR, so excluding the name at sync
# time is enough — no separate webhook-side filter is needed (init
# containers are always skipped, see webhook_patch._CONTAINER_PATHS).
#
# Resolution: same workload > namespace > helm-default chain as the rest.
# An explicit empty string at the workload layer is a deliberate "clear
# the namespace's list" override (e.g. one Deployment in `production`
# wants `istio-proxy` managed even though the Namespace excludes it).
SKIP_CONTAINERS_KEY = "skipContainers"

# OOM-aware per-workload opt-out. Defaults to the chart's
# `config.oomDetectionEnabled`; operator can flip to "false" on a specific
# workload that does its own GC dance and shouldn't trigger auto-bumps.
# Resolution: same workload > namespace > helm-default chain as
# `autoRollout` and the value-bearing keys.
OOM_DETECTION_ENABLED_KEY = "oomDetectionEnabled"

# OOM floor stickiness. Defaults to the chart's
# `config.oomFloorEnabled` (default true). False on workload/namespace
# means: bump still fires on a fresh OOM (immediate help), but the
# resulting limit is NOT recorded as a sticky floor — the next sync's
# Prom-driven recommendation is free to drop the limit again. Use case:
# workloads where Prom is the source of truth for sizing and an
# operator-curated floor would be in the way of normal sizing-down
# after an optimization.
# Resolution: workload > namespace > helm.
OOM_FLOOR_ENABLED_KEY = "oomFloorEnabled"

# One-shot floor reset. When set to "true" on a
# Namespace OR workload, the next sync clears that workload's
# `oom-floor.<container>` / `oom-last-event.<container>` /
# `oom-boost-history.<container>` annotations on every container
# of every CR in that scope. The annotation has no helm default and
# is intentionally NOT removed by the tool (the reset is recorded in
# the annotation's audit history; operator deletes the marker
# manually after confirming the reset). Use case: workload was
# optimized to use less memory, the sticky floor from previous OOMs
# is now blocking the lower recommendation.
OOM_FLOOR_RESET_KEY = "oomFloorReset"

# Bool literals accepted as truthy. Mirrors src/config.py:_bool — keep them
# in lockstep so an annotation and a ConfigMap value parse the same way.
_TRUE_LITERALS = ("1", "true", "yes")
_FALSE_LITERALS = ("0", "false", "no", "")

# All Config / ResourceConfig fields that can be overridden per-namespace or
# per-workload. The mapping value is a (target, attribute, parser) triple:
#   target:    "config" or "resource" (which dataclass holds the field)
#   attribute: dataclass field name to write
#   parser:    callable(str) → typed value
#
# helm-only keys deliberately absent here: prometheusUrl, crWriteback.*,
# gitlabUsername, gitAuthorName, gitAuthorEmail, logLevel, logFormat. Those
# are cluster-wide and don't make sense to flip per-workload.
def _parse_bool(s: str) -> bool:
    s = (s or "").strip().lower()
    if s in _TRUE_LITERALS:
        return True
    if s in _FALSE_LITERALS:
        return False
    raise ValueError(f"expected boolean (true/false/yes/no/1/0), got {s!r}")


def _parse_float(s: str) -> float:
    return float(s)


def _parse_int(s: str) -> int:
    return int(s)


def _parse_str(s: str) -> str:
    return s


# (target, field, parser) per camelCase key. "target" is the dataclass that
# owns the attribute; "field" is its Python attribute name (snake_case).
_KEY_SPEC: dict[str, tuple[str, str, Any]] = {
    # Mode flags
    "growOnly":              ("config",   "grow_only",              _parse_bool),
    "shrinkOnly":            ("config",   "shrink_only",            _parse_bool),
    "dryRun":                ("config",   "dry_run",                _parse_bool),
    "createMr":              ("config",   "create_mr",              _parse_bool),
    # Source / windows
    "cpuPercentile":         ("resource", "cpu_percentile",         _parse_float),
    "memPercentile":         ("resource", "mem_percentile",         _parse_float),
    "cpuRequestWindow":      ("resource", "cpu_request_window",     _parse_str),
    "memRequestWindow":      ("resource", "mem_request_window",     _parse_str),
    "cpuLimitWindow":        ("resource", "cpu_limit_window",       _parse_str),
    "memLimitWindow":        ("resource", "mem_limit_window",       _parse_str),
    # Margins
    "marginFraction":        ("resource", "margin_fraction",        _parse_float),
    "cpuRequestMargin":      ("resource", "cpu_request_margin",     _parse_float),
    "memRequestMargin":      ("resource", "mem_request_margin",     _parse_float),
    "cpuLimitMargin":        ("resource", "cpu_limit_margin",       _parse_float),
    "memLimitMargin":        ("resource", "mem_limit_margin",       _parse_float),
    # Multipliers / rounding
    "cpuLimitMultiplier":    ("resource", "cpu_limit_multiplier",   _parse_float),
    "memoryLimitMultiplier": ("resource", "memory_limit_multiplier", _parse_float),
    "roundValues":           ("resource", "round_values",           _parse_bool),
    # Per-resource bounds (0 = disabled, mirrors ResourceConfig defaults)
    "minCpuRequestM":        ("resource", "min_cpu_request_m",      _parse_int),
    "minMemoryRequestMi":    ("resource", "min_memory_request_mi",  _parse_int),
    "maxCpuRequestM":        ("resource", "max_cpu_request_m",      _parse_int),
    "maxMemoryRequestMi":    ("resource", "max_memory_request_mi",  _parse_int),
    "maxCpuLimitM":          ("resource", "max_cpu_limit_m",        _parse_int),
    "maxMemoryLimitMi":      ("resource", "max_memory_limit_mi",    _parse_int),
    # Floor for emitted limits — lives on Config (not ResourceConfig) because
    # legacy code path passes these as standalone parameters to writeback.
    "minCpuLimitM":          ("config",   "min_cpu_limit_m",        _parse_int),
    "minMemoryLimitMi":      ("config",   "min_memory_limit_mi",    _parse_int),
    # Cold-start CPU floor — applied when a freshly-OOMed container has no
    # Prometheus history (writeback_webhook.py:922 reads `rc.cold_start_cpu_floor_m`
    # off the resolved config). Lives on ResourceConfig; same helm < ns <
    # workload override chain as the bounds above. (Bug #56.)
    "coldStartCpuFloorM":    ("resource", "cold_start_cpu_floor_m", _parse_int),
    # OOM-aware bump multiplier — applied to the memory limit when a container
    # OOMs (writeback_webhook.py reads `rc.oom_bump_factor` off the resolved
    # config). On ResourceConfig; documented as a per-workload override in
    # docs/reference.md but was absent from this map, so its annotation was
    # silently dropped — same bug class as the coldStartCpuFloorM gap above.
    "oomBumpFactor":         ("resource", "oom_bump_factor",         _parse_float),
}

# Public set of key names callers can introspect (e.g. for a `--list-keys`
# CLI subcommand or for documentation generation in the chart README).
KNOWN_KEYS: frozenset[str] = frozenset(
    {*_KEY_SPEC.keys(), OPT_IN_KEY, SKIP_KEY, AUTO_ROLLOUT_KEY,
     SKIP_CONTAINERS_KEY, OOM_DETECTION_ENABLED_KEY,
     OOM_FLOOR_ENABLED_KEY, OOM_FLOOR_RESET_KEY},
)


def _parse_skip_containers(raw: str) -> list[str]:
    """Parse a CSV of container names. Strips whitespace, drops empties.

    Empty input → empty list (the explicit "clear the inherited list"
    workload override). Order is preserved for log readability; the
    consumer dedups by treating it as a set when filtering.
    """
    if not raw:
        return []
    return [name.strip() for name in raw.split(",") if name.strip()]


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def parse_annotations(
    annotations: Mapping[str, str] | None,
    *,
    scope: str,
    source: str = "annotation",
) -> dict[str, Any]:
    """Extract `kube-resource-updater.*` keys into a typed dict.

    `scope` is "namespace" or "workload" — used to validate that
    `enabled` only appears on namespaces and `skip` only on workloads.
    `source` shows up in warning messages so an operator can locate
    the offending annotation quickly (e.g. `source="ns/n8n"`).

    Returns a plain dict keyed by the **camelCase** annotation name (without
    the prefix). Values are typed per `_KEY_SPEC`. Unknown keys emit a
    warning but never raise; we'd rather process the rest of the workload
    than fail closed on a typo. Malformed values (e.g. `cpuPercentile: foo`)
    are dropped with a warning for the same reason.
    """
    out: dict[str, Any] = {}
    if not annotations:
        return out

    for raw_key, raw_val in annotations.items():
        if not raw_key.startswith(ANNOTATION_PREFIX):
            continue
        key = raw_key[len(ANNOTATION_PREFIX):]

        if key == OPT_IN_KEY:
            if scope != "namespace":
                _log.warning(
                    "[overrides] %s: annotation '%s' is only valid on a Namespace; ignored on %s",
                    source, raw_key, scope,
                )
                continue
            try:
                out[key] = _parse_bool(raw_val)
            except ValueError as exc:
                _log.warning("[overrides] %s: %s=%r — %s; ignored", source, raw_key, raw_val, exc)
            continue

        if key == OOM_DETECTION_ENABLED_KEY:
            # Same shape as autoRollout — boolean, valid on namespace
            # AND workload, hierarchy resolution via dedicated helper.
            try:
                out[key] = _parse_bool(raw_val)
            except ValueError as exc:
                _log.warning("[overrides] %s: %s=%r — %s; ignored", source, raw_key, raw_val, exc)
            continue

        if key == OOM_FLOOR_ENABLED_KEY:
            # Same hierarchy resolution as oomDetectionEnabled — see
            # `is_oom_floor_enabled`. Boolean only.
            try:
                out[key] = _parse_bool(raw_val)
            except ValueError as exc:
                _log.warning("[overrides] %s: %s=%r — %s; ignored", source, raw_key, raw_val, exc)
            continue

        if key == OOM_FLOOR_RESET_KEY:
            # One-shot opt-in. Read by the slow-path bump logic, which
            # zeroes prior OOM state for every container of every CR in
            # this scope. The annotation itself is left in place — the
            # reset entry in `oom-boost-history.<container>` records the
            # action; operator removes the annotation manually.
            try:
                out[key] = _parse_bool(raw_val)
            except ValueError as exc:
                _log.warning("[overrides] %s: %s=%r — %s; ignored", source, raw_key, raw_val, exc)
            continue

        if key == AUTO_ROLLOUT_KEY:
            # Valid on namespace and workload (hierarchy: workload > ns > helm).
            # Not a Config field — the webhook reads this directly via
            # is_auto_rollout_enabled() at CR-event time.
            try:
                out[key] = _parse_bool(raw_val)
            except ValueError as exc:
                _log.warning("[overrides] %s: %s=%r — %s; ignored", source, raw_key, raw_val, exc)
            continue

        if key == SKIP_CONTAINERS_KEY:
            # Valid on namespace and workload (hierarchy: workload > ns > helm).
            # Empty string is deliberate — workload uses it to clear an
            # inherited namespace list. Stored as a list so the order in
            # the annotation is preserved in logs.
            out[key] = _parse_skip_containers(raw_val)
            continue

        if key == SKIP_KEY:
            if scope != "workload":
                _log.warning(
                    "[overrides] %s: annotation '%s' is only valid on a workload; ignored on %s",
                    source, raw_key, scope,
                )
                continue
            try:
                out[key] = _parse_bool(raw_val)
            except ValueError as exc:
                _log.warning("[overrides] %s: %s=%r — %s; ignored", source, raw_key, raw_val, exc)
            continue

        spec = _KEY_SPEC.get(key)
        if spec is None:
            _log.warning(
                "[overrides] %s: unknown annotation '%s' — typo? known keys: %s",
                source, raw_key, ", ".join(sorted(KNOWN_KEYS)),
            )
            continue

        _, _, parser = spec
        try:
            out[key] = parser(raw_val)
        except (TypeError, ValueError) as exc:
            _log.warning("[overrides] %s: %s=%r — %s; ignored", source, raw_key, raw_val, exc)
            continue
    return out


def merge(*layers: Mapping[str, Any]) -> dict[str, Any]:
    """Right-most layer wins. Keys absent in a layer fall through.

    Convenience helper for the `helm → ns → workload` order: callers do
    `merge(parse_annotations(ns), parse_annotations(workload))` to get the
    final override dict, then call `apply()` to project it onto a Config.
    Helm defaults are not merged here — they live on the base Config the
    caller passes to `apply()`.
    """
    out: dict[str, Any] = {}
    for layer in layers:
        if layer:
            out.update(layer)
    return out


def apply(base: Config, overrides: Mapping[str, Any]) -> Config:
    """Return a copy of `base` with `overrides` applied.

    Only fields listed in `_KEY_SPEC` are touched. `OPT_IN_KEY` and `SKIP_KEY`
    are filtering signals (used by the caller to decide whether to process a
    workload at all), not config fields, so they're skipped here.

    Validation is delegated to the dataclass itself: a bad parser output
    that nonetheless type-checks (e.g. negative percentile) will silently
    take effect. The Prometheus query layer is responsible for rejecting
    nonsense values at use-time; centralising that here would just duplicate
    the logic.
    """
    if not overrides:
        return base

    # Bucket overrides by target dataclass. We only build a new ResourceConfig
    # if at least one resource-scoped key is set — otherwise base.resource
    # is shared by reference (cheap, immutable from the tool's POV).
    config_overrides: dict[str, Any] = {}
    resource_overrides: dict[str, Any] = {}

    for key, value in overrides.items():
        if key in (OPT_IN_KEY, SKIP_KEY, AUTO_ROLLOUT_KEY, SKIP_CONTAINERS_KEY,
                   OOM_DETECTION_ENABLED_KEY, OOM_FLOOR_ENABLED_KEY,
                   OOM_FLOOR_RESET_KEY):
            continue
        spec = _KEY_SPEC.get(key)
        if spec is None:
            # Already warned during parse_annotations; defensive skip in case
            # the caller hand-built the dict.
            continue
        target, attr, _ = spec
        if target == "config":
            config_overrides[attr] = value
        elif target == "resource":
            resource_overrides[attr] = value

    new_resource = (
        dataclasses.replace(base.resource, **resource_overrides)
        if resource_overrides else base.resource
    )

    if not config_overrides and new_resource is base.resource:
        return base

    return dataclasses.replace(base, resource=new_resource, **config_overrides)


def is_namespace_enabled(annotations: Mapping[str, str] | None) -> bool:
    """True if the Namespace carries `kube-resource-updater.enabled: "true"`.

    The single source of opt-in for the sync loop and the
    webhook code-path filter. No fallback to labels — if you want to opt
    in, set the annotation.
    """
    if not annotations:
        return False
    val = annotations.get(ANNOTATION_PREFIX + OPT_IN_KEY, "")
    try:
        return _parse_bool(val)
    except ValueError:
        return False


def is_workload_skipped(annotations: Mapping[str, str] | None) -> bool:
    """True if the workload carries `kube-resource-updater.skip: "true"`."""
    if not annotations:
        return False
    val = annotations.get(ANNOTATION_PREFIX + SKIP_KEY, "")
    try:
        return _parse_bool(val)
    except ValueError:
        return False


def is_auto_rollout_enabled(
    helm_default: bool,
    namespace_annotations: Mapping[str, str] | None,
    workload_annotations: Mapping[str, str] | None,
) -> bool:
    """Resolve the auto-rollout decision through the workload > namespace > helm chain.

    The first layer that explicitly sets the annotation wins. A malformed
    value is logged and treated as "not set" so a typo at one layer falls
    through to the next instead of silently flipping the decision.
    """
    return _resolve_bool_chain(
        AUTO_ROLLOUT_KEY, helm_default,
        namespace_annotations, workload_annotations,
    )


def is_oom_detection_enabled(
    helm_default: bool,
    namespace_annotations: Mapping[str, str] | None,
    workload_annotations: Mapping[str, str] | None,
) -> bool:
    """Resolve OOM detection eligibility through workload > namespace > helm.

    Mirrors `is_auto_rollout_enabled`: first layer that explicitly sets the
    annotation wins, malformed values fall through to the next layer.
    Returns the helm default when neither layer sets it.
    """
    return _resolve_bool_chain(
        OOM_DETECTION_ENABLED_KEY, helm_default,
        namespace_annotations, workload_annotations,
    )


def is_oom_floor_enabled(
    helm_default: bool,
    namespace_annotations: Mapping[str, str] | None,
    workload_annotations: Mapping[str, str] | None,
) -> bool:
    """Resolve floor stickiness through workload > namespace > helm.

    False at any layer means: bump still fires on a fresh OOM (one-shot
    help) but the resulting limit is NOT recorded as a floor — the next
    sync's Prom-driven recommendation can drop the limit again.
    """
    return _resolve_bool_chain(
        OOM_FLOOR_ENABLED_KEY, helm_default,
        namespace_annotations, workload_annotations,
    )


def is_oom_floor_reset_requested(
    namespace_annotations: Mapping[str, str] | None,
    workload_annotations: Mapping[str, str] | None,
) -> bool:
    """One-shot reset opt-in. No helm default — explicit annotation only.

    Returns True if either layer sets the annotation truthy. Order
    doesn't matter for a one-shot — namespace-wide reset and
    workload-targeted reset are equivalent for the affected workload.
    """
    full_key = ANNOTATION_PREFIX + OOM_FLOOR_RESET_KEY
    for source in (workload_annotations, namespace_annotations):
        if source is None:
            continue
        raw = source.get(full_key)
        if raw is None:
            continue
        try:
            if _parse_bool(raw):
                return True
        except ValueError as exc:
            _log.warning(
                "[overrides] %s=%r — %s; ignored",
                full_key, raw, exc,
            )
    return False


def _resolve_bool_chain(
    key: str,
    helm_default: bool,
    namespace_annotations: Mapping[str, str] | None,
    workload_annotations: Mapping[str, str] | None,
) -> bool:
    """Shared implementation of the workload > namespace > helm boolean
    resolution. First layer that parses cleanly wins; malformed values
    fall through with a warning.
    """
    full_key = ANNOTATION_PREFIX + key
    for source in (workload_annotations, namespace_annotations):
        if source is None:
            continue
        raw = source.get(full_key)
        if raw is None:
            continue
        try:
            return _parse_bool(raw)
        except ValueError as exc:
            _log.warning(
                "[overrides] %s=%r — %s; falling through to next layer",
                full_key, raw, exc,
            )
            continue
    return helm_default


def resolve_skip_containers(
    helm_default: list[str] | None,
    namespace_annotations: Mapping[str, str] | None,
    workload_annotations: Mapping[str, str] | None,
) -> list[str]:
    """Resolve the skipContainers list through the workload > namespace > helm chain.

    The first layer that explicitly sets the annotation wins — including the
    deliberate "" value, which clears the inherited list (e.g. a Namespace
    sets `skipContainers: "istio-proxy"` and one workload opts back in by
    setting `skipContainers: ""` on its own metadata). This matches the
    "explicit beats inherited" semantics of `is_auto_rollout_enabled`.

    Returns a list (order preserved from the annotation) so the caller can
    log it; dedup happens at filter time by converting to a set.
    """
    full_key = ANNOTATION_PREFIX + SKIP_CONTAINERS_KEY
    for source in (workload_annotations, namespace_annotations):
        if source is None:
            continue
        raw = source.get(full_key)
        if raw is None:
            continue
        return _parse_skip_containers(raw)
    return list(helm_default or [])


def resolve_for_workload(
    base: Config,
    namespace_annotations: Mapping[str, str] | None,
    workload_annotations: Mapping[str, str] | None,
    *,
    namespace_name: str = "",
    workload_name: str = "",
) -> Config:
    """End-to-end shortcut: parse → merge (ns then workload) → apply.

    Most callers want this; the lower-level functions are exposed for tests
    and for the webhook hot path where the Namespace layer is cached and
    pre-parsed (avoid re-parsing on every admission).
    """
    ns_overrides = parse_annotations(
        namespace_annotations,
        scope="namespace",
        source=f"ns/{namespace_name}" if namespace_name else "namespace",
    )
    wl_overrides = parse_annotations(
        workload_annotations,
        scope="workload",
        source=f"{namespace_name}/{workload_name}" if workload_name else "workload",
    )
    return apply(base, merge(ns_overrides, wl_overrides))
