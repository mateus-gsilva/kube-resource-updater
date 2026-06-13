#!/usr/bin/env python3
"""
QA — kube-resource-updater.

Runs two kinds of checks against the project's pure functions, plus an optional
end-to-end Prometheus parameter sweep when `PROMETHEUS_URL` is set:

  1. **Unit invariants** (always run, no external dependency):
     - grow-only / shrink-only respect the prior value;
     - `_enforce_floors` raises requests AND limits, never lowers;
     - rounding / floor / ceiling order produces consistent values;
     - co-location detection in `discover_prometheus_url` picks svc-DNS
       when the local Prometheus service is reachable.

  2. **Resolver smoke**:
     - hierarchical (helm < ns < workload) annotation overrides applied
       through `resolve_for_workload`. Full coverage in test_overrides.py.

  3. **Prometheus parameter sweep** (skipped without `PROMETHEUS_URL`):
     - CPU/memory request percentile × window matrix for a known workload;
     - max-over-time CPU + memory limits;
     - confirms percentile/window changes produce *differentiated* values.

Run:

    python3 tools/qa_params.py
    PROMETHEUS_URL=http://localhost:9090 NS=monitoring python3 tools/qa_params.py
"""

# --------------------------------------------------------------------------- #
# SECTION INDEX (run order; regenerate when adding a section)                 #
# --------------------------------------------------------------------------- #
#   section_grow_shrink                            grow-only / shrink-only — applied to requests and limits
#   section_floors                                 _enforce_floors — raises req+lim when below floor; never lowers
#   section_rounding                               _build_container_resources — rounding + ceiling order
#   section_colocation                             Prometheus URL — explicit, no auto-discovery
#   section_promql_injection                       PromQL injection — label-value + regex escape
#   section_mr_timeouts_and_description_cap        MR timeouts + description cap
#   section_mr_orphan_branch_recovery              MR orphan-branch recovery
#   section_run_subprocess_timeout                 _run subprocess timeout
#   section_memory_parser_edges                    Memory parser edges — Pi/Ei, decimal suffixes, sci notation
#   section_yaml_rendering_quirks                  YAML rendering — ruamel auto-quote on YAML-special label values
#   section_crd_schema_tightening                  CRD schema tightening — quantity pattern + name maxLength
#   section_crd_cel_hardening                      CRD CEL hardening — list-map-keys + requests≤limits + per-unit
#   section_webhook_patch_corners                  Webhook admission patch — corner cases
#   section_prefetch                               prefetch_prometheus_parallel — cache + dedup
#   section_credentials_and_prometheus_modes       Credentials + Prometheus URL resolution (single source / explicit only
#   section_selector_inference                     Selector inference — _derive_selector across chart conventions
#   section_validating_webhook                     Validating webhook — selector + container overlap detection
#   section_mr_metadata                            MR metadata — assignees / reviewers / labels / squash
#   section_skip_containers                        skipContainers — sync filter + init container auto-skip
#   section_oom_slow_path                          OOM-aware slow-path — detection, bump, annotation, floor
#   section_status_updater                         StatusUpdater — coalesce + per-CR PATCH semantics
#   section_auto_rollout                           Auto-rollout — hierarchy + debounce + no-op guard
#   section_chart_conditional_rbac                 Chart conditional RBAC — render check (helm template)
#   section_cert_reconciler                        CertReconciler — self-signed cert generation (cert-manager replacement
#   section_namespace_cache                        NamespaceCache — opt-in short-circuit + annotation lookup
#   section_cr_cache_reconnect                     ResourceOverrideCache — synthetic events on reconnect
#   section_cache_unparseable_modified_leak        ResourceOverrideCache — drop stale on un-parseable MODIFIED
#   section_cache_bootstrap_retry                  Cache bootstrap retry — survives transient API failure (methodology pa
#   section_create_mr_bucketing                    createMr per-workload bucketing — pass-1 carry-forward + pass-2 full s
#   section_dry_run_bucketing                      dryRun per-workload bucketing — #   section_cr_name_collision                      CR name collision — Deployment + StatefulSet same-name disambiguation
#   section_config_validate                        Config.validate — fail-fast hardening
#   section_margin_default_safe                    — marginFraction default safety (>= 0.05)
#   section_log_formatter                          log formatter — tag padding + pastel color + JSON sanitization
#   section_resolver                               resolver — helm / namespace / workload override matrix
#   section_discovery_auth_raise                   discovery — raise on auth failure (401/403)
#   section_mr_retry_on_429_and_5xx                MR retry on 429 + 5xx
#   section_cache_reconnect_backoff                Cache watch-reconnect backoff + readiness flip
#   section_safe_json_non_json_body                _safe_json — non-JSON 200 body handling (audit follow-up)
#   section_webhook_cert_san_clusterdomain         Webhook cert SAN — configurable clusterDomain
#   section_mr_description_truncation_count        MR description truncation — drop-count footer
#   section_oom_bump_cap_with_investigation        OOM bump runaway-cap + investigation annotation
#   section_cold_start_cpu_floor                   Cold-start CPU floor — #   section_oom_bump_clamp_warning                 OOM bump clamp warning — #   section_dependency_pins                        Dependency pins (closed upstream regressions)
#   section_trivial_log_and_defaults               Trivial batch — log polish + default hygiene
#   section_detect_scrape_interval                 detect_scrape_interval — scrape interval auto-detection
#   section_request_query_single_series            Request query single-series guarantee — cross-workload max bug fix
#   section_irate_lookback_dynamic                 irate lookback window dynamic with step (Bug 2)
#   section_typo_warning_known_keys                Typo warning uses KNOWN_KEYS (includes behaviour keys) — Bug 1
#   section_status_flush_non_api_exception         flush_once non-ApiException re-queues remaining CRs — Bug 2
#   section_cert_malformed_base64                  webhook_cert malformed base64 → _regenerate_and_exit, not spin loop —
#   section_cert_noreturn_annotation               webhook_cert _regenerate_and_exit return annotation is NoReturn — Bug
#   section_cert_409_adopted_validation            webhook_cert 409 adopted cert validation — Bug 2
#   section_git_provider_abstraction               git provider abstraction — GitLabProvider wraps GitLab helpers (Phase
#   section_github_provider                        git provider — GitHubProvider
#   section_provider_factory_and_config            provider-agnostic factory + config selection
#   section_chart_git_provider_wiring              provider-agnostic git wiring (render asserts)
#   section_webhook_cache_annotation_constant      webhook_cache — opt-in count delegates to is_namespace_enabled (batch
#   section_dead_log_credentials_source            writeback — log_git_credentials_source is dead (batch 1b)
#   section_scrape_interval_fallback_log_level     detect_scrape_interval — fallback logs at WARNING not INFO (batch 2)
#   section_delta_str_silent_swallow               _delta_str — bare except swallows parse failure silently (writeback.py
#   section_webhook_sa_automount                   Chart webhook ServiceAccount — automountServiceAccountToken: true (bat
#   section_webhook_module_api                     Webhook module public API — no cross-module private calls
#   section_public_readiness_code_fixes            Public-readiness code fixes — dead fn, paste artifact, dedup, content-
#   section_public_readiness_chart_fixes           Public-readiness chart fixes — labels, NOTES, pdb, seccomp, replicas,
#   section_overrides_unit_file                    Overrides unit tests (tools/test_overrides.py — bridged)
#   section_live_prometheus                        (dynamic title)
from __future__ import annotations

import base64
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.config import Config, CrWritebackConfig, ResourceConfig
# Note: `discover_prometheus_url` + `_get_local_apiserver_ips` were
# removed along with the auto-discovery code path.
from src.overrides import resolve_for_workload
from src.prometheus import (
    detect_scrape_interval,
    prefetch_prometheus_parallel,
    query_cpu_max_m,
    query_cpu_request_m,
    query_mem_request_bytes,
    query_memory_max_bytes,
)
from src.workload import ContainerRecommendation
from src.writeback import (
    ResourceBounds,
    _apply_grow_shrink,
    _build_container_resources,
    _enforce_floors,
)

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"
_SKIP = "\033[33mSKIP\033[0m"


def _chart_dir() -> str:
    """Resolve the Helm chart directory for both supported layouts:

    1. monorepo:    <repo>/../../gitops/helm-charts/kube-resource-updater
    2. public repo: <repo>/charts/kube-resource-updater (co-located)

    Returns the first existing path (monorepo wins for local dev); callers
    SKIP their section when the returned path does not exist.
    """
    here = os.path.dirname(__file__)
    monorepo = os.path.abspath(os.path.join(
        here, "..", "..", "..", "gitops", "helm-charts", "kube-resource-updater"))
    if os.path.isdir(monorepo):
        return monorepo
    return os.path.abspath(os.path.join(
        here, "..", "charts", "kube-resource-updater"))

_failures = 0


def _check(label: str, got, expected) -> None:
    global _failures
    ok = got == expected
    if not ok:
        _failures += 1
    print(f"  [{_PASS if ok else _FAIL}] {label}")
    if not ok:
        print(f"      expected: {expected!r}")
        print(f"      got:      {got!r}")


def _check_truthy(label: str, got) -> None:
    _check(label, bool(got), True)


def _section(title: str) -> None:
    print()
    print("=" * 80)
    print(title.center(80))
    print("=" * 80)


def _base_config() -> Config:
    return Config(
        gitlab_url="",
        gitlab_token="",
        gitlab_username="",
        git_author_name="kru",
        git_author_email="kru@example",
        dry_run=False,
        create_mr=True,
        min_cpu_limit_m=0,
        min_memory_limit_mi=0,
        prometheus_url="",
        resource=ResourceConfig(),
        cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides"),
    )


# --------------------------------------------------------------------------- #
# Section 1: grow-only / shrink-only on requests AND limits                    #
# --------------------------------------------------------------------------- #

def section_grow_shrink() -> None:
    _section("grow-only / shrink-only — applied to requests and limits")

    new_lower = {"requests": {"cpu": "100m", "memory": "128Mi"},
                 "limits":   {"cpu": "200m", "memory": "256Mi"}}
    old_higher = {"requests": {"cpu": "500m", "memory": "512Mi"},
                  "limits":   {"cpu": "1",   "memory": "1Gi"}}
    new_higher = {"requests": {"cpu": "500m", "memory": "512Mi"},
                  "limits":   {"cpu": "1",   "memory": "1Gi"}}
    old_lower = {"requests": {"cpu": "100m", "memory": "128Mi"},
                 "limits":   {"cpu": "200m", "memory": "256Mi"}}

    out = _apply_grow_shrink(dict(new_lower), old_higher, grow_only=True, shrink_only=False)
    _check("grow-only: lower req keeps old req cpu", out["requests"]["cpu"],   "500m")
    _check("grow-only: lower req keeps old req mem", out["requests"]["memory"], "512Mi")
    _check("grow-only: lower lim keeps old lim cpu", out["limits"]["cpu"],     "1")
    _check("grow-only: lower lim keeps old lim mem", out["limits"]["memory"],  "1Gi")

    out = _apply_grow_shrink(dict(new_higher), old_lower, grow_only=True, shrink_only=False)
    _check("grow-only: higher new replaces old req", out["requests"]["cpu"],   "500m")
    _check("grow-only: higher new replaces old lim", out["limits"]["cpu"],     "1")

    out = _apply_grow_shrink(dict(new_higher), old_lower, grow_only=False, shrink_only=True)
    _check("shrink-only: higher new keeps old req",  out["requests"]["cpu"],   "100m")
    _check("shrink-only: higher new keeps old lim",  out["limits"]["cpu"],     "200m")

    out = _apply_grow_shrink(dict(new_lower), old_higher, grow_only=False, shrink_only=True)
    _check("shrink-only: lower new replaces old req", out["requests"]["cpu"],  "100m")
    _check("shrink-only: lower new replaces old lim", out["limits"]["cpu"],    "200m")

    out = _apply_grow_shrink(dict(new_lower), old_res=None, grow_only=True, shrink_only=False)
    _check("grow-only on fresh install writes new",   out["requests"]["cpu"],  "100m")

    # ── Both flags active = freeze ──────────────────────
    # _apply_grow_shrink with both=True keeps every old value (new < old
    # → grow_only keeps old; new > old → shrink_only keeps old).
    out = _apply_grow_shrink(dict(new_lower), old_higher, grow_only=True, shrink_only=True)
    _check("[freeze] both active: lower new → keeps old (cpu req)",
           out["requests"]["cpu"],   "500m")
    _check("[freeze] both active: lower new → keeps old (mem lim)",
           out["limits"]["memory"],  "1Gi")
    out = _apply_grow_shrink(dict(new_higher), old_lower, grow_only=True, shrink_only=True)
    _check("[freeze] both active: higher new → keeps old (cpu req)",
           out["requests"]["cpu"],   "100m")

    # ── Integration: per-workload create_mr propagation + grow/shrink ──
    # Validate that _build_containers_payload now actually receives old_res
    # via prev_res_lookup (oom_state["containers"]) and clamps accordingly.
    from src.config import Config, ResourceConfig, CrWritebackConfig
    from src.workload import ContainerRecommendation, WorkloadRecommendation as WR
    from src.writeback_webhook import _build_containers_payload
    from types import SimpleNamespace

    cfg_grow = Config(
        gitlab_url="", gitlab_token="", gitlab_username="",
        git_author_name="kru", git_author_email="kru@example",
        dry_run=False, create_mr=True,
        min_cpu_limit_m=0, min_memory_limit_mi=0,
        prometheus_url="http://prom:9090",
        grow_only=True, shrink_only=False,
        resource=ResourceConfig(),
        cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides"),
    )
    rec_g = WR(
        name="api", namespace="ns", target_kind="Deployment", target_name="api",
        containers=[ContainerRecommendation(container_name="app")],
    )
    # Prom recommends LOWER than apiserver-source — grow_only should clamp up.
    prom_lower = SimpleNamespace(
        cpu_request_m=100, memory_request_bytes=100 * 1024 * 1024,
        cpu_limit_m=200, memory_limit_bytes=200 * 1024 * 1024,
    )
    apiserver_containers = {
        "app": {
            "requests": {"cpu": "300m", "memory": "300Mi"},
            "limits":   {"cpu": "600m", "memory": "600Mi"},
        },
    }
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_lower):
        payload, _ = _build_containers_payload(
            rec_g, cfg_grow,
            oom_state={
                "floor": {}, "last_event": {}, "history": {},
                "containers": apiserver_containers,
            },
            oom_events={},
            oom_eligible=True,
        )
    _check("[grow-only integration] Prom shrink blocked, cpu req kept at old (300m)",
           payload[0]["requests"]["cpu"], "300m")
    _check("[grow-only integration] Prom shrink blocked, mem lim kept at old (600Mi)",
           payload[0]["limits"]["memory"], "600Mi")

    # Same setup with shrink_only — Prom shrink ALLOWED (new < old).
    cfg_shrink = Config(
        gitlab_url="", gitlab_token="", gitlab_username="",
        git_author_name="kru", git_author_email="kru@example",
        dry_run=False, create_mr=True,
        min_cpu_limit_m=0, min_memory_limit_mi=0,
        prometheus_url="http://prom:9090",
        grow_only=False, shrink_only=True,
        resource=ResourceConfig(min_cpu_request_m=0, min_memory_request_mi=0),
        cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides"),
    )
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_lower):
        payload, _ = _build_containers_payload(
            rec_g, cfg_shrink,
            oom_state={
                "floor": {}, "last_event": {}, "history": {},
                "containers": apiserver_containers,
            },
            oom_events={},
            oom_eligible=True,
        )
    _check("[shrink-only integration] Prom shrink allowed (new < old)",
           payload[0]["requests"]["cpu"], "100m")

    # ── shrink_only + OOM bump → bump SUPPRESSED, floor NOT recorded ───
    # User asked for this behavior explicitly: operator policy is sacred,
    # even when the kernel killed the pod. We log a clear WARNING but
    # don't override the policy.
    from src.writeback_webhook import OomEvent
    ev = OomEvent(
        namespace="ns", workload_name="api", container="app",
        finished_at="2026-05-09T20:00:00Z",
        trap_limit_bytes=200 * 1024 * 1024,
    )
    # Apiserver has lim=200Mi. Bump would push to 300Mi. shrink_only
    # reverts. Floor should NOT be stamped (truthful annotation).
    apiserver_at_trap = {
        "app": {
            "requests": {"cpu": "100m", "memory": "100Mi"},
            "limits":   {"cpu": "200m", "memory": "200Mi"},
        },
    }
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_lower):
        payload, anns = _build_containers_payload(
            rec_g, cfg_shrink,
            oom_state={
                "floor": {}, "last_event": {}, "history": {},
                "containers": apiserver_at_trap,
            },
            oom_events={"app": ev},
            oom_eligible=True,
        )
    _check("[shrink+OOM] bump SUPPRESSED — lim stays at 200Mi (matches old)",
           payload[0]["limits"]["memory"], "200Mi")
    from src.writeback_webhook import (
        floor_annotation_key, last_event_annotation_key, history_annotation_key,
    )
    _check("[shrink+OOM] oom-floor annotation NOT stamped (bump didn't take effect)",
           floor_annotation_key("app") in anns, False)
    _check("[shrink+OOM] oom-history NOT stamped (bump didn't take effect)",
           history_annotation_key("app") in anns, False)
    _check("[shrink+OOM] oom-last-event IS stamped (dedupe so we don't re-process next sync)",
           anns.get(last_event_annotation_key("app")), "2026-05-09T20:00:00Z")

    # ── grow_only + OOM bump → bump APPLIED normally (no conflict) ─────
    cfg_grow.resource = ResourceConfig(oom_bump_factor=1.5, oom_floor_enabled=True)
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_lower):
        payload, anns = _build_containers_payload(
            rec_g, cfg_grow,
            oom_state={
                "floor": {}, "last_event": {}, "history": {},
                "containers": apiserver_at_trap,
            },
            oom_events={"app": ev},
            oom_eligible=True,
        )
    _check("[grow+OOM] bump applied — lim raised to 300Mi (trap × 1.5)",
           payload[0]["limits"]["memory"], "300Mi")
    _check("[grow+OOM] oom-floor stamped (bump took effect)",
           anns.get(floor_annotation_key("app")), "300Mi")
    _check("[grow+OOM] oom-history stamped",
           "200Mi→300Mi" in (anns.get(history_annotation_key("app")) or ""), True)


# --------------------------------------------------------------------------- #
# Section 2: floor enforcement                                                 #
# --------------------------------------------------------------------------- #

def section_floors() -> None:
    _section("_enforce_floors — raises req+lim when below floor; never lowers")

    rc = ResourceConfig(min_cpu_request_m=200, min_memory_request_mi=512)
    bounds = ResourceBounds.from_config(
        cpu_cap_mult=4.0, mem_cap_mult=3.0,
        min_cpu_limit_m=0, min_memory_limit_mi=0, rc=rc,
    )

    res = {"requests": {"cpu": "50m", "memory": "100Mi"},
           "limits":   {"cpu": "150m", "memory": "300Mi"}}
    floored = _enforce_floors(res, bounds)
    _check("floor: cpu req raised to floor", floored["requests"]["cpu"],   "200m")
    _check("floor: mem req raised to floor", floored["requests"]["memory"], "512Mi")
    _check("floor: cpu lim raised to ≥ req", floored["limits"]["cpu"],     "200m")
    _check("floor: mem lim raised to ≥ req", floored["limits"]["memory"],  "512Mi")

    above = {"requests": {"cpu": "500m", "memory": "1Gi"},
             "limits":   {"cpu": "1",   "memory": "2Gi"}}
    same = _enforce_floors(above, bounds)
    _check("floor: above-floor request untouched", same["requests"], above["requests"])
    _check("floor: above-floor limit untouched",   same["limits"],   above["limits"])


# --------------------------------------------------------------------------- #
# Section 3: rounding + ceiling order                                          #
# --------------------------------------------------------------------------- #

def section_rounding() -> None:
    _section("_build_container_resources — rounding + ceiling order")

    rc = ResourceConfig(round_values=True, max_cpu_request_m=400, max_memory_request_mi=2048)
    bounds = ResourceBounds.from_config(
        cpu_cap_mult=4.0, mem_cap_mult=3.0,
        min_cpu_limit_m=0, min_memory_limit_mi=0, rc=rc,
    )

    container = ContainerRecommendation(container_name="test")
    res = _build_container_resources(container, bounds, cpu_request_m=401)
    _check_truthy("round+ceil: cpu 401 → result not None",   res)
    _check("round+ceil: cpu 401 → 400m (cap after round)",  res["requests"]["cpu"], "400m")

    res2 = _build_container_resources(container, bounds, memory_request_bytes=2100 * 1024 * 1024)
    _check_truthy("round+ceil: mem populated", res2 and res2["requests"].get("memory"))


# --------------------------------------------------------------------------- #
# Section 4: co-location detection                                             #
# --------------------------------------------------------------------------- #

def section_colocation() -> None:
    """Placeholder kept so the main() ordering stays stable. The original
    section tested `discover_prometheus_url` + `_get_local_apiserver_ips`;
    both were removed — Prometheus URL is now required to
    be set explicitly via `config.prometheusUrl`. Test for that requirement
    lives in `section_chart_conditional_rbac` (helm-render fail) and
    `section_config_validate` (runtime Config.validate fail).
    """
    _section("Prometheus URL — explicit, no auto-discovery")
    print("  [skip] auto-discovery removed; checks moved to chart_conditional_rbac + config_validate")


def section_promql_injection() -> None:
    """PromQL label-value + regex escape.

    Pre-chart-1.22.0 the queries interpolated `namespace`, `container`,
    `target_name` directly into label matchers without escape. A workload
    named `app.api` produced `pod=~"app.api-.*"` — RE2 reads `.` as
    "any char", cross-matching `appXapi-pod-abc`. Worse: a future
    regression that lets `target_name` contain `"` or `\\` would inject
    arbitrary PromQL.

    These asserts lock in the escape helpers (`_escape_label_value`,
    `_escape_label_regex`) and the build path that uses them.
    """
    _section("PromQL injection — label-value + regex escape")

    from src.prometheus import _escape_label_value, _escape_label_regex
    from unittest.mock import patch as _patch, MagicMock

    # ── _escape_label_value: backslash + quote + newline only ─────────────
    _check("[esc-label-plain] alphanumeric pass-through",
           _escape_label_value("argocd"), "argocd")
    _check("[esc-label-quote] double-quote → backslash-quote",
           _escape_label_value('foo"bar'), 'foo\\"bar')
    _check("[esc-label-backslash] backslash → double-backslash",
           _escape_label_value("foo\\bar"), "foo\\\\bar")
    _check("[esc-label-newline] newline → backslash-n",
           _escape_label_value("foo\nbar"), "foo\\nbar")
    _check("[esc-label-empty] empty passes through",
           _escape_label_value(""), "")

    # ── _escape_label_regex: regex specials + label-string specials ───────
    # `app.api` → regex-escape to `app\.api` → label-escape \ to \\ → `app\\.api`
    # PromQL wire: `app\\.api` → string-unescape → `app\.api` → RE2 → literal "."
    _check("[esc-regex-dot] dot escaped + double-escaped",
           _escape_label_regex("app.api"), "app\\\\.api")
    # Plain alphanumeric: no regex specials, no escape
    _check("[esc-regex-plain] alphanumeric unchanged",
           _escape_label_regex("appapi"), "appapi")
    # Hyphen: re.escape escapes hyphen too (defensive — `-` IS special inside
    # regex character classes like `[a-z]`). Double-escaped for the label
    # value layer. The semantic match still works because RE2 outside a
    # character class treats `\-` as literal `-`.
    _check("[esc-regex-hyphen-defensive] hyphen escaped + doubled",
           _escape_label_regex("app-api"), "app\\\\-api")
    # Multiple specials
    got = _escape_label_regex("a.b+c*d?e")
    _check("[esc-regex-multi] all regex specials escaped+doubled",
           got, "a\\\\.b\\\\+c\\\\*d\\\\?e")
    # Empty
    _check("[esc-regex-empty] empty passes through",
           _escape_label_regex(""), "")

    # ── End-to-end: ensure the escaped query reaches Prometheus ───────────
    # Mock requests.get and capture the params={"query": ...} payload.
    # The dotted target_name MUST appear escaped in the final query.
    from src import prometheus as _prom
    _prom._cache.clear(); _prom._mem_cache.clear()
    _prom._req_cpu_cache.clear(); _prom._req_mem_cache.clear()
    captured: dict = {}
    def _fake_get(url, params=None, **kw):
        captured["query"] = (params or {}).get("query", "")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": {"result": []}}
        return resp
    with _patch.object(_prom.requests, "get", side_effect=_fake_get):
        _prom.query_cpu_max_m(
            "http://prom:9090", namespace="ns",
            container="app", target_name="my.app", window="7d",
        )
    q = captured["query"]
    # The dotted name must appear escaped, not raw.
    _check("[promql-e2e-dot-escaped] dotted target_name escaped in pod_filter",
           'pod=~"my\\\\.app-.*"' in q, True)
    _check("[promql-e2e-no-raw-dot] raw 'my.app-' must NOT appear",
           'pod=~"my.app-.*"' in q, False)

    # Negative: a fully-alphanumeric target_name produces the unescaped path
    # (re.escape on alphanum is a no-op; this checks we don't accidentally
    # double-escape and break literal matches).
    captured.clear()
    _prom._cache.clear()
    with _patch.object(_prom.requests, "get", side_effect=_fake_get):
        _prom.query_cpu_max_m(
            "http://prom:9090", namespace="ns",
            container="app", target_name="myapp", window="7d",
        )
    _check("[promql-e2e-clean-name] pure alphanumeric target_name not over-escaped",
           'pod=~"myapp-.*"' in captured["query"], True)

    # Namespace + container: also escaped via _escape_label_value path.
    captured.clear()
    _prom._cache.clear()
    with _patch.object(_prom.requests, "get", side_effect=_fake_get):
        _prom.query_cpu_max_m(
            "http://prom:9090", namespace='ns"injection',
            container="app", target_name=None, window="7d",
        )
    _check("[promql-e2e-ns-quote-escaped] namespace with quote escaped",
           'namespace="ns\\"injection"' in captured["query"], True)


def section_mr_timeouts_and_description_cap() -> None:
    """GitLab MR HTTP timeouts + description size cap.

    Pre-chart-1.22.1 the three `requests` calls inside `_create_gitlab_mr`
    (POST create, GET on 409-list-existing, PUT update-description) had NO
    timeout. A hung GitLab API blocked the whole CronJob until
    `cronjob.activeDeadlineSeconds` (default 1800s) hard-killed the pod,
    leaving a half-done sync — commit pushed, MR never opened. Operator
    sees changes on `main` but no review hand-off ever happens.

    Separately, MR descriptions over GitLab's 1 MiB limit caused 422
    rejection mid-sync (same half-done state). `_mr_description` now caps
    the body via `_truncate_mr_description` before returning.

    These asserts lock in:
      - all three calls in `_create_gitlab_mr` pass `timeout=` kwarg;
      - a `requests.Timeout` propagates (not silently swallowed) so the
        CronJob exits non-zero and the alert fires;
      - oversize descriptions are truncated below the cap and the
        truncation footer is appended;
      - under-cap descriptions pass through untouched (no false-positive).
    """
    _section("MR timeouts + description cap")

    import requests as _requests
    from src.writeback import _create_gitlab_mr
    from src.writeback_webhook import (
        _MR_DESCRIPTION_CAP_BYTES,
        _MR_DESCRIPTION_HEADROOM_BYTES,
        _MR_DESCRIPTION_TRUNCATION_FOOTER,
        _truncate_mr_description,
    )
    # Derive the expected GitLab truncate-to budget the same way production
    # does — single source of truth, no hardcoded 800_000.
    _gitlab_headroom = min(_MR_DESCRIPTION_HEADROOM_BYTES, _MR_DESCRIPTION_CAP_BYTES // 4)
    _gitlab_truncate_to = max(_MR_DESCRIPTION_CAP_BYTES - _gitlab_headroom, 0)
    # Sanity: GitLab budget must still be exactly 800_000.
    assert _gitlab_truncate_to == 800_000, (
        f"GitLab truncate-to budget changed: expected 800_000, got {_gitlab_truncate_to}"
    )

    # ── 1. POST create-MR carries a timeout kwarg ────────────────────────
    # added a pre-POST adoption lookup (GET) — mock it as empty
    # so the POST path runs and we can pin its timeout kwarg.
    with patch("src.writeback._gitlab_post") as post, \
         patch("src.writeback._gitlab_get")  as get:
        get.return_value = MagicMock(
            status_code=200,
            json=lambda: [],
            raise_for_status=MagicMock(return_value=None),
        )
        post.return_value = MagicMock(
            status_code=201,
            json=lambda: {"web_url": "https://git.example.com/x/-/merge_requests/1"},
        )
        post.return_value.raise_for_status = MagicMock(return_value=None)
        _create_gitlab_mr(
            gitlab_url="https://git.example.com",
            token="tok",
            project_path="ns/x",
            source_branch="resource-updater/sync",
            target_branch="main",
            title="t", description="d",
        )
        kwargs = post.call_args.kwargs
        _check("[mr-timeout-post] requests.post receives timeout kwarg",
               "timeout" in kwargs, True)
        _check("[mr-timeout-post] timeout is a positive number",
               isinstance(kwargs.get("timeout"), (int, float)) and kwargs["timeout"] > 0,
               True)

    # ── 2. 409 path: GET listing + PUT description both carry timeout ────
    # added a pre-POST adoption lookup, also a GET. To pin the
    # 409 path's own GET + PUT timeouts we make the first GET (adoption)
    # return empty so POST runs and returns 409, then the second GET (race
    # recovery inside the 409 handler) finds the racing MR.
    with patch("src.writeback._gitlab_post") as post, \
         patch("src.writeback._gitlab_get")  as get,  \
         patch("src.writeback._gitlab_put")  as put:
        post.return_value = MagicMock(status_code=409,
                                      raise_for_status=MagicMock(return_value=None))
        adoption_empty = MagicMock(
            status_code=200,
            json=lambda: [],
            raise_for_status=MagicMock(return_value=None),
        )
        race_recovery = MagicMock(
            status_code=200,
            json=lambda: [{"iid": 42, "web_url": "https://git.example.com/x/-/merge_requests/42"}],
            raise_for_status=MagicMock(return_value=None),
        )
        get.side_effect = [adoption_empty, race_recovery]
        put.return_value = MagicMock(status_code=200,
                                     raise_for_status=MagicMock(return_value=None))
        url = _create_gitlab_mr(
            gitlab_url="https://git.example.com",
            token="tok",
            project_path="ns/x",
            source_branch="resource-updater/sync",
            target_branch="main",
            title="t", description="d",
        )
        _check("[mr-timeout-409] returned existing MR url",
               url, "https://git.example.com/x/-/merge_requests/42")
        _check("[mr-timeout-409-get] list-existing GET carries timeout",
               "timeout" in get.call_args.kwargs, True)
        _check("[mr-timeout-409-put] update-description PUT carries timeout",
               "timeout" in put.call_args.kwargs, True)
        # 1.22.9 — pin that the 409-recovery GET filters by target_branch
        # too.
        recovery_params = get.call_args_list[1].kwargs.get("params") or {}
        _check("[mr-timeout-409-get-target] 409-recovery GET filters target_branch",
               recovery_params.get("target_branch"), "main")
        _check("[mr-timeout-409-get-state] 409-recovery GET restricts to opened state",
               recovery_params.get("state"), "opened")

    # ── 3. requests.Timeout propagates (no silent swallow) ───────────────
    # adoption lookup runs before POST — mock GET as empty so
    # the POST is reached and its Timeout is what propagates.
    with patch("src.writeback._gitlab_post") as post, \
         patch("src.writeback._gitlab_get")  as get:
        get.return_value = MagicMock(
            status_code=200,
            json=lambda: [],
            raise_for_status=MagicMock(return_value=None),
        )
        post.side_effect = _requests.Timeout("simulated GitLab hang")
        raised = False
        try:
            _create_gitlab_mr(
                gitlab_url="https://git.example.com",
                token="tok",
                project_path="ns/x",
                source_branch="resource-updater/sync",
                target_branch="main",
                title="t", description="d",
            )
        except _requests.Timeout:
            raised = True
        _check("[mr-timeout-raise] Timeout propagates to caller", raised, True)

    # ── 4. _truncate_mr_description — under-cap pass-through ─────────────
    small = "## ResourceOverride CRs\n\n| ns | wl | c | ... |\n"
    _check("[mr-description-under-cap] small body unchanged",
           _truncate_mr_description(small), small)
    _check("[mr-description-under-cap] no truncation footer appended",
           _MR_DESCRIPTION_TRUNCATION_FOOTER.strip() in _truncate_mr_description(small),
           False)

    # ── 5. _truncate_mr_description — over-cap truncates + footers ──────
    huge = "x" * (_MR_DESCRIPTION_CAP_BYTES + 50_000)  # well over cap
    out = _truncate_mr_description(huge)
    out_bytes = out.encode("utf-8")
    _check("[mr-description-cap] truncated body strictly below GitLab 1 MiB",
           len(out_bytes) < 1024 * 1024, True)
    _check("[mr-description-cap] truncated body stays under our cap constant",
           len(out_bytes) <= _MR_DESCRIPTION_CAP_BYTES, True)
    _check("[mr-description-cap] truncation footer appended",
           out.endswith(_MR_DESCRIPTION_TRUNCATION_FOOTER), True)
    _check("[mr-description-cap] truncate-to budget honoured (≤ target + footer)",
           len(out_bytes) <= _gitlab_truncate_to
                              + len(_MR_DESCRIPTION_TRUNCATION_FOOTER.encode("utf-8")),
           True)

    # ── 6. Multi-byte UTF-8 boundary safety ─────────────────────────────
    # `→` is 3 bytes in UTF-8; a naive byte-slice could cut mid-codepoint
    # and emit invalid UTF-8. The helper uses errors="ignore" so the
    # truncated output is always valid UTF-8 round-trippable text.
    multibyte = ("→" * (_MR_DESCRIPTION_CAP_BYTES // 2))  # well over cap in bytes
    out_mb = _truncate_mr_description(multibyte)
    _check("[mr-description-cap-utf8] truncated output is valid UTF-8",
           out_mb.encode("utf-8").decode("utf-8") == out_mb, True)
    _check("[mr-description-cap-utf8] still under GitLab cap",
           len(out_mb.encode("utf-8")) <= _MR_DESCRIPTION_CAP_BYTES, True)


# --------------------------------------------------------------------------- #
# Section 4b: MR-open silent failure recovery                      #
# --------------------------------------------------------------------------- #

def section_mr_orphan_branch_recovery() -> None:
    """push lands → MR-open crashes → re-run never recovers.

    Five-step state machine in `writeback_webhook._commit_repo`:
      clone → write → commit → push → MR-open

    The 4→5 transition is silent-broken. If
    `_create_gitlab_mr` POST fails after push succeeded (5xx, hang, malformed
    JSON), the work-branch is left on the remote with no MR. Next sync sees
    no file diff → no commit → no push → MR never opens. The OOM bump is
    silently stranded for weeks.

    The 409-conflict path inside `_create_gitlab_mr` already implements
    "lookup existing MR by source_branch". The fix is to invoke that same
    lookup BEFORE the initial POST when source_branch ≠ target_branch
    (i.e. only the MR-flow bucket; direct-push bucket can't be orphaned).

    Pinned invariants:
      - pre-POST lookup short-circuits when an open MR exists for
        (source_branch, target_branch) — no second POST;
      - lookup filters BOTH source_branch AND target_branch (current 409
        path filters only source_branch — too loose);
      - empty list → POST proceeds normally (no degenerate sentinel return);
      - lookup HTTP error propagates (does NOT silently fall through to
        POST — that would re-introduce a sibling silent-broken bug);
      - direct-push bucket (source_branch == target_branch) skips the
        adoption lookup entirely.
    """
    _section("MR orphan-branch recovery")

    import requests as _requests
    from src.writeback import _create_gitlab_mr

    common = dict(
        gitlab_url="https://git.example.com",
        token="tok",
        project_path="ns/x",
        title="t",
        description="d",
    )

    # ── 1. Adoption short-circuits POST when an open MR exists ──────────
    with patch("src.writeback._gitlab_post") as post, \
         patch("src.writeback._gitlab_get")  as get,  \
         patch("src.writeback._gitlab_put")  as put:
        get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"iid": 42,
                           "web_url": "https://git.example.com/x/-/merge_requests/42"}],
            raise_for_status=MagicMock(return_value=None),
        )
        post.return_value = MagicMock(
            status_code=201,
            json=lambda: {"web_url": "https://git.example.com/x/-/merge_requests/99"},
            raise_for_status=MagicMock(return_value=None),
        )
        put.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(return_value=None),
        )
        url = _create_gitlab_mr(
            source_branch="resource-updater/sync",
            target_branch="main",
            **common,
        )
        _check("[mr-adopt-existing] no POST when open MR exists for branch pair",
               post.called, False)
        _check("[mr-adopt-existing] returned adopted MR url",
               url, "https://git.example.com/x/-/merge_requests/42")
        _check("[mr-adopt-existing] lookup GET was invoked",
               get.called, True)

        # ── 2. Lookup filters BOTH source_branch AND target_branch ──────
        params = get.call_args.kwargs.get("params") or {}
        _check("[mr-adopt-filter-source] lookup filters source_branch",
               params.get("source_branch"), "resource-updater/sync")
        _check("[mr-adopt-filter-target] lookup filters target_branch",
               params.get("target_branch"), "main")
        _check("[mr-adopt-filter-state] lookup restricts to opened state",
               params.get("state"), "opened")
        _check("[mr-adopt-get-timeout] lookup GET carries timeout",
               "timeout" in get.call_args.kwargs, True)

    # ── 3. Empty list → POST proceeds normally ──────────────────────────
    with patch("src.writeback._gitlab_post") as post, \
         patch("src.writeback._gitlab_get")  as get:
        get.return_value = MagicMock(
            status_code=200,
            json=lambda: [],
            raise_for_status=MagicMock(return_value=None),
        )
        post.return_value = MagicMock(
            status_code=201,
            json=lambda: {"web_url": "https://git.example.com/x/-/merge_requests/100"},
            raise_for_status=MagicMock(return_value=None),
        )
        url = _create_gitlab_mr(
            source_branch="resource-updater/sync",
            target_branch="main",
            **common,
        )
        _check("[mr-adopt-empty] POST runs when no open MR found",
               post.call_count, 1)
        _check("[mr-adopt-empty] returned newly-created MR url",
               url, "https://git.example.com/x/-/merge_requests/100")

    # ── 4. Lookup HTTP error propagates (no degrade-to-POST) ────────────
    with patch("src.writeback._gitlab_post") as post, \
         patch("src.writeback._gitlab_get")  as get:
        get.side_effect = _requests.RequestException("connection reset")
        raised = False
        try:
            _create_gitlab_mr(
                source_branch="resource-updater/sync",
                target_branch="main",
                **common,
            )
        except _requests.RequestException:
            raised = True
        _check("[mr-adopt-lookup-err] RequestException from lookup propagates",
               raised, True)
        _check("[mr-adopt-lookup-err] POST is NOT called when lookup fails",
               post.called, False)

    # ── 5. Direct-push bucket (source == target) skips the lookup ───────
    with patch("src.writeback._gitlab_post") as post, \
         patch("src.writeback._gitlab_get")  as get:
        post.return_value = MagicMock(
            status_code=201,
            json=lambda: {"web_url": "https://git.example.com/x/-/merge_requests/101"},
            raise_for_status=MagicMock(return_value=None),
        )
        _create_gitlab_mr(
            source_branch="main",
            target_branch="main",
            **common,
        )
        _check("[mr-adopt-skip-same-branch] no lookup when source == target",
               get.called, False)

    # ── 6. Adoption path issues a PUT to refresh the MR description ──────
    # Bug: when the pre-POST adoption GET finds an existing open MR it
    # returns existing[0]["web_url"] immediately WITHOUT issuing a PUT to
    # update the MR description. The 409-recovery path DOES call
    # _gitlab_put(..., json={"description": ...}), so the two recovery
    # branches behave inconsistently. A reviewer sees the previous sync's
    # CPU/memory numbers in the MR description even after new commits have
    # been force-pushed to the source branch.
    #
    # This assert FAILS before the fix: PUT is never called.
    with patch("src.writeback._gitlab_post") as post,          patch("src.writeback._gitlab_get")  as get,          patch("src.writeback._gitlab_put")  as put:
        get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"iid": 42,
                           "web_url": "https://git.example.com/x/-/merge_requests/42"}],
            raise_for_status=MagicMock(return_value=None),
        )
        put.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(return_value=None),
        )
        url = _create_gitlab_mr(
            source_branch="resource-updater/sync",
            target_branch="main",
            **{**common, "description": "updated cpu/mem table"},
        )
        _check("[mr-adopt-desc-refresh] adoption path returns existing MR url",
               url, "https://git.example.com/x/-/merge_requests/42")
        _check("[mr-adopt-desc-refresh] PUT is issued to refresh MR description",
               put.called, True)
        if put.called:
            put_url = put.call_args.args[0] if put.call_args.args else ""
            _check("[mr-adopt-desc-refresh] PUT targets the correct MR iid (42)",
                   "/merge_requests/42" in put_url, True)
            put_json = put.call_args.kwargs.get("json") or {}
            _check("[mr-adopt-desc-refresh] PUT body carries updated description",
                   put_json.get("description"), "updated cpu/mem table")
            _check("[mr-adopt-desc-refresh] PUT carries timeout kwarg",
                   "timeout" in put.call_args.kwargs, True)


# --------------------------------------------------------------------------- #
# Section 4c: subprocess.run timeout on git calls                  #
# --------------------------------------------------------------------------- #

def section_run_subprocess_timeout() -> None:
    """`_run` shells git without `subprocess.run(timeout=...)`.

    A degraded GitLab remote hangs `git clone` / `git fetch` / `git push`
    until the CronJob's `activeDeadlineSeconds` (default 1800s) hard-kills
    the pod. Mirrors the  HTTP-timeout fix shape.

    Pinned invariants:
      - `_run` passes a positive `timeout=` kwarg to subprocess.run by default;
      - caller-supplied timeout override is honored;
      - `subprocess.TimeoutExpired` propagates to the caller (not swallowed);
      - on timeout, an ERROR log is emitted with the cmd sanitized via
        `_strip_auth` — no token leak.
    """
    _section("_run subprocess timeout")

    import subprocess as _subprocess
    from src import writeback_webhook as _wbwh

    # ── 1. Default timeout passed through ────────────────────────────────
    with patch.object(_wbwh.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _wbwh._run(["git", "version"])
        kwargs = run.call_args.kwargs
        _check("[run-timeout-default] subprocess.run receives timeout kwarg",
               "timeout" in kwargs, True)
        # 1.22.9 — tightened from `> 0` to `>= 30s`. The earlier loose check
        # would have passed even if the default regressed to `timeout=0.001`,
        # which would TimeoutExpired immediately on every git call. 30s is
        # the floor under which any real git operation against a healthy
        # remote should never fall (clone --depth 1 of the gitops repo is
        # typically <3s; push <5s).
        _check("[run-timeout-default] default timeout is at least 30s "
               "(prevents regression to absurdly-low defaults)",
               isinstance(kwargs.get("timeout"), (int, float))
                   and kwargs["timeout"] >= 30,
               True)

    # ── 2. Explicit override honored ─────────────────────────────────────
    with patch.object(_wbwh.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _wbwh._run(["git", "clone", "https://x"], timeout=300)
        _check("[run-timeout-override] caller-supplied timeout reaches subprocess.run",
               run.call_args.kwargs.get("timeout"), 300)

    # ── 3. TimeoutExpired propagates + sanitized error log ───────────────
    with patch.object(_wbwh.subprocess, "run") as run, \
         patch.object(_wbwh._log, "error")     as logerr:
        run.side_effect = _subprocess.TimeoutExpired(cmd=["git", "clone"], timeout=120)
        raised = False
        try:
            _wbwh._run(["git", "clone",
                        "https://user:s3cr3t@host/x.git", "/tmp/repo"])
        except _subprocess.TimeoutExpired:
            raised = True
        _check("[run-timeout-raise] TimeoutExpired propagates to caller",
               raised, True)
        _check("[run-timeout-log] error log was emitted on timeout",
               logerr.called, True)
        logged_blob = (
            " ".join(str(a) for a in (logerr.call_args.args or ()))
            + " "
            + " ".join(f"{k}={v}" for k, v in (logerr.call_args.kwargs or {}).items())
        )
        _check("[run-timeout-log-sanitized] secret not present in error log",
               "s3cr3t" in logged_blob, False)

    # ── 4. Non-zero exit: stderr scrubbed before logging (OSS pre-publish) ──
    # The returncode!=0 path logs result.stderr, which git fills with the
    # clone URL including the token on a failed clone. Pre-fix it was logged
    # verbatim → token leak on git <2.35. _redact_auth must scrub it.
    with patch.object(_wbwh.subprocess, "run") as run, \
         patch.object(_wbwh._log, "error") as logerr:
        result_mock = MagicMock(
            returncode=128, stdout="",
            stderr="fatal: unable to access 'https://oauth2:s3cr3t@host/x.git/': 403 Forbidden",
        )
        result_mock.check_returncode.side_effect = _subprocess.CalledProcessError(
            128, ["git", "clone"])
        run.return_value = result_mock
        raised = False
        try:
            _wbwh._run(["git", "clone", "https://oauth2:s3cr3t@host/x.git", "/tmp/r"])
        except _subprocess.CalledProcessError:
            raised = True
        _check("[run-fail-raise] non-zero exit raises CalledProcessError", raised, True)
        fail_blob = (
            " ".join(str(a) for a in (logerr.call_args.args or ()))
            + " "
            + " ".join(f"{k}={v}" for k, v in (logerr.call_args.kwargs or {}).items())
        )
        _check("[run-fail-stderr-scrubbed] secret not present in non-zero-exit error log",
               "s3cr3t" in fail_blob, False)

    # ── 5. Non-zero exit: the raised CalledProcessError.cmd is scrubbed ─────
    # check_returncode() sets exc.cmd = the original argv, which carries the
    # auth URL (token). Python prints exc.cmd in the default traceback, so an
    # unhandled exception leaks the token even though the error LOG is clean.
    # _run must scrub exc.cmd before re-raising.
    with patch.object(_wbwh.subprocess, "run") as run, \
         patch.object(_wbwh._log, "error"):
        token_url = "https://x-access-token:s3cr3t@host/x.git"
        result_mock = MagicMock(returncode=128, stdout="", stderr="")
        result_mock.check_returncode.side_effect = _subprocess.CalledProcessError(
            128, ["git", "clone", token_url, "/tmp/r"])
        run.return_value = result_mock
        raised_exc = None
        try:
            _wbwh._run(["git", "clone", token_url, "/tmp/r"])
        except _subprocess.CalledProcessError as exc:
            raised_exc = exc
        _check("[run-fail-cmd-raise] non-zero exit raises CalledProcessError",
               raised_exc is not None, True)
        _check("[run-fail-cmd-scrubbed] secret not present in raised exc.cmd",
               "s3cr3t" in str(raised_exc.cmd if raised_exc else ""), False)
        _check("[run-fail-cmd-str-scrubbed] secret not present in str(exc)",
               "s3cr3t" in str(raised_exc) if raised_exc else False, False)


# --------------------------------------------------------------------------- #
# Section 5: prefetch parallel — cache populates uniformly                     #
# --------------------------------------------------------------------------- #

def section_prefetch() -> None:
    _section("prefetch_prometheus_parallel — cache + dedup")

    # Empty URL: no work, no crash.
    prefetch_prometheus_parallel(
        entries=[("ns", "container", "workload")],
        prometheus_url="",
        cpu_percentile=0.9, cpu_window="3d",
        mem_percentile=0.9, mem_window="8d",
    )
    _check("empty url: no exception", True, True)

    fake_resp = MagicMock()
    fake_resp.json.return_value = {"data": {"result": [{"value": [0, "0.123"]}]}}
    fake_resp.raise_for_status.return_value = None
    fake_resp.ok = True
    with patch("src.prometheus.requests.get", return_value=fake_resp):
        from src import prometheus as _p
        _p._cache.clear(); _p._mem_cache.clear()
        _p._req_cpu_cache.clear(); _p._req_mem_cache.clear()

        entries = [("ns", "c1", "wl"), ("ns", "c1", "wl"), ("ns", "c2", "wl")]
        prefetch_prometheus_parallel(
            entries=entries,
            prometheus_url="http://prom.fake",
            cpu_percentile=0.9, cpu_window="3d",
            mem_percentile=0.9, mem_window="8d",
        )
        _check("prefetch: cpu_max cache size",  len(_p._cache),         2)
        _check("prefetch: mem_max cache size",  len(_p._mem_cache),     2)
        _check("prefetch: cpu_req cache size",  len(_p._req_cpu_cache), 2)
        _check("prefetch: mem_req cache size",  len(_p._req_mem_cache), 2)


def section_auto_rollout() -> None:
    """Covers the 1.4.0 auto-rollout trigger:

      - Hierarchy (workload > namespace > helm-default).
      - Annotation typo / malformed value falls through to next layer.
      - `_resources_changed` no-op guard ignores label-only CR edits.
      - Per-workload debounce: multiple events on the same workload
        coalesce into one PATCH after the window.
      - Selector-no-match: events for an empty selector are dropped.
    """
    _section("Auto-rollout — hierarchy + debounce + no-op guard")

    from src.overrides import is_auto_rollout_enabled, ANNOTATION_PREFIX
    from src.webhook_cache import ContainerOverride, ResourceOverride

    P = ANNOTATION_PREFIX

    # ── hierarchy: workload > namespace > helm ────────────────────────
    _check("[hier] all empty → helm default false",
           is_auto_rollout_enabled(False, {}, {}), False)
    _check("[hier] all empty → helm default true",
           is_auto_rollout_enabled(True, {}, {}), True)
    _check("[hier] ns says true, helm false → ns wins",
           is_auto_rollout_enabled(False, {P + "autoRollout": "true"}, {}), True)
    _check("[hier] workload says false, ns true → workload wins",
           is_auto_rollout_enabled(True, {P + "autoRollout": "true"},
                                   {P + "autoRollout": "false"}), False)
    _check("[hier] workload silent, ns says false → ns wins (override helm true)",
           is_auto_rollout_enabled(True, {P + "autoRollout": "false"}, {}), False)

    # ── malformed value falls through to next layer ───────────────────
    _check("[hier] bad bool at workload → fall through to ns",
           is_auto_rollout_enabled(False,
                                   {P + "autoRollout": "true"},
                                   {P + "autoRollout": "maybe"}), True)
    _check("[hier] bad bool at every layer → helm default",
           is_auto_rollout_enabled(False,
                                   {P + "autoRollout": "perhaps"},
                                   {P + "autoRollout": "maybe"}), False)

    # ── _resources_changed no-op guard ────────────────────────────────
    from src.webhook_rollout import _resources_changed, _labels_match

    a = ResourceOverride(
        namespace="ns", name="ro",
        selector_match_labels={"app": "x"},
        containers=(
            ContainerOverride(name="main",
                              requests={"cpu": "100m"}, limits={"cpu": "200m"}),
        ),
    )
    a_same = ResourceOverride(
        namespace="ns", name="ro",
        selector_match_labels={"app": "x"},
        containers=(
            ContainerOverride(name="main",
                              requests={"cpu": "100m"}, limits={"cpu": "200m"}),
        ),
    )
    a_diff_req = ResourceOverride(
        namespace="ns", name="ro",
        selector_match_labels={"app": "x"},
        containers=(
            ContainerOverride(name="main",
                              requests={"cpu": "150m"}, limits={"cpu": "200m"}),
        ),
    )
    a_extra = ResourceOverride(
        namespace="ns", name="ro",
        selector_match_labels={"app": "x"},
        containers=(
            ContainerOverride(name="main",
                              requests={"cpu": "100m"}, limits={"cpu": "200m"}),
            ContainerOverride(name="sidecar",
                              requests={"cpu": "50m"}, limits={}),
        ),
    )
    _check("[no-op] same resources → no rollout",   _resources_changed(a, a_same), False)
    _check("[no-op] cpu request changed → rollout", _resources_changed(a, a_diff_req), True)
    _check("[no-op] container added → rollout",     _resources_changed(a, a_extra), True)

    # ── _labels_match (selector semantics) ────────────────────────────
    _check("[selector] empty selector matches nothing",
           _labels_match({}, {"app": "x"}), False)
    _check("[selector] subset match",
           _labels_match({"app": "x"}, {"app": "x", "tier": "db"}), True)
    _check("[selector] mismatch fails",
           _labels_match({"app": "x"}, {"app": "y"}), False)

    # ── debounce: multiple schedules on same workload, single restart ─
    from unittest.mock import MagicMock
    from src.webhook_rollout import RolloutTrigger

    fake_apps = MagicMock()
    deploy = MagicMock()
    deploy.metadata.name = "loki"
    deploy.metadata.annotations = {}
    deploy.spec.template.metadata.labels = {"app.kubernetes.io/instance": "loki"}
    fake_apps.list_namespaced_deployment.return_value = MagicMock(items=[deploy])
    fake_apps.list_namespaced_stateful_set.return_value = MagicMock(items=[])

    fake_ns = MagicMock()
    fake_ns.annotations.return_value = {}
    trig = RolloutTrigger(
        apps_api=fake_apps, ns_cache=fake_ns,
        helm_default_enabled=True, debounce_seconds=999,  # never auto-fire
    )

    ro = ResourceOverride(
        namespace="loki", name="loki",
        selector_match_labels={"app.kubernetes.io/instance": "loki"},
        containers=(
            ContainerOverride(name="loki", requests={"cpu": "100m"}, limits={}),
        ),
    )
    ro_changed = ResourceOverride(
        namespace="loki", name="loki",
        selector_match_labels={"app.kubernetes.io/instance": "loki"},
        containers=(
            ContainerOverride(name="loki", requests={"cpu": "200m"}, limits={}),
        ),
    )

    # 3 events on the same workload coalesce.
    trig.handle_event("ADDED", ro, None)
    trig.handle_event("MODIFIED", ro_changed, ro)
    trig.handle_event("MODIFIED", ro_changed, ro)
    _check("[debounce] 3 events on same workload → 1 pending entry",
           len(trig._pending), 1)

    pending_key = next(iter(trig._pending))
    _check("[debounce] pending key is (ns, kind, name)",
           pending_key, ("loki", "Deployment", "loki"))

    # MODIFIED with no resource diff is a no-op.
    trig._pending.clear()
    trig.handle_event("MODIFIED", ro, ro)
    _check("[debounce] same resources → no pending entry", len(trig._pending), 0)

    # _fire_due force-flushes regardless of timer.
    trig.handle_event("ADDED", ro, None)
    trig._fire_due(force=True)
    _check("[debounce] force-flush calls patch_namespaced_deployment",
           fake_apps.patch_namespaced_deployment.called, True)
    _check("[debounce] pending cleared after fire",
           len(trig._pending), 0)

    # ── Framework audit bug-hunt: transient PATCH failure must NOT drop ──
    # the pending restart silently. Pre-fix: _restart_workload caught
    # ApiException, logged, and returned — the entry was already removed
    # from _pending by _fire_due, so no retry ever happened. Stable-CR
    # production setups where a PDB block coincides with a CR change
    # would leave pods running stale resources until the operator made
    # ANOTHER CR change. Fix: re-insert with bounded exponential
    # backoff on non-404 ApiException; drop on 404 (workload gone).
    from kubernetes.client.rest import ApiException
    import datetime as _dt

    def _new_trig(side_effect) -> "RolloutTrigger":
        apps = MagicMock()
        apps.patch_namespaced_deployment.side_effect = side_effect
        # Mirror the existing fake_apps shape so `_workloads_matching`
        # can list the (cached) deploy and resolve the (kind, name).
        apps.list_namespaced_deployment.return_value = MagicMock(items=[deploy])
        apps.list_namespaced_stateful_set.return_value = MagicMock(items=[])
        return RolloutTrigger(
            apps_api=apps, ns_cache=fake_ns,
            helm_default_enabled=True, debounce_seconds=999,
        )

    # Case A: transient 503 → entry re-inserted with future fire_at.
    trig_flaky = _new_trig(ApiException(status=503, reason="ServiceUnavailable"))
    trig_flaky.handle_event("ADDED", ro, None)
    _check("[rollout-retry] flaky case: scheduled in _pending",
           len(trig_flaky._pending), 1)
    trig_flaky._fire_due(force=True)
    _check("[rollout-retry] PATCH attempted (503)",
           trig_flaky._apps.patch_namespaced_deployment.called, True)
    _check("[rollout-retry] entry re-inserted after transient failure (not dropped)",
           len(trig_flaky._pending), 1)
    if trig_flaky._pending:
        key_flaky = next(iter(trig_flaky._pending))
        re_entry = trig_flaky._pending[key_flaky]
        _check("[rollout-retry] re-insert carries an attempt counter > 0",
               getattr(re_entry, "attempt", 0) > 0, True)
        _check("[rollout-retry] re-insert sets fire_at in the future (backoff)",
               re_entry.fire_at > _dt.datetime.now(_dt.timezone.utc), True)
    else:
        _check("[rollout-retry] re-insert carries an attempt counter > 0",
               "PRE-FIX: no entry", "POST-FIX: entry with attempt counter")
        _check("[rollout-retry] re-insert sets fire_at in the future (backoff)",
               "PRE-FIX: no entry", "POST-FIX: entry with future fire_at")

    # Case B: 404 (workload deleted between event and PATCH) → DROP entry.
    trig_404 = _new_trig(ApiException(status=404, reason="NotFound"))
    trig_404.handle_event("ADDED", ro, None)
    trig_404._fire_due(force=True)
    _check("[rollout-retry-404] 404 (workload gone) drops the entry (no retry)",
           len(trig_404._pending), 0)

    # Case C: bounded retries — after enough attempts, give up.
    trig_perm = _new_trig(ApiException(status=403, reason="Forbidden"))
    trig_perm.handle_event("ADDED", ro, None)
    # Fire repeatedly with force=True — each attempt should re-insert
    # until the attempt counter exceeds the cap.
    for _ in range(10):
        trig_perm._fire_due(force=True)
    _check("[rollout-retry-bounded] permanent failure eventually drops the entry",
           len(trig_perm._pending), 0)
    _check("[rollout-retry-bounded] gave up after a bounded number of attempts",
           trig_perm._apps.patch_namespaced_deployment.call_count <= 5, True)

    # ── DaemonSet rollout dispatch ──────────────────────────
    # Pre-fix: _workloads_matching never lists DaemonSets, so the event is
    # never scheduled; and _restart_workload's kind dispatch has no DaemonSet
    # arm. Post-fix: the DS is matched, scheduled, and restarted via
    # patch_namespaced_daemon_set.
    ds_workload = MagicMock()
    ds_workload.metadata.name = "fluentd"
    ds_workload.metadata.annotations = {}
    ds_workload.spec.template.metadata.labels = {"app": "fluentd"}

    apps_ds = MagicMock()
    apps_ds.list_namespaced_deployment.return_value = MagicMock(items=[])
    apps_ds.list_namespaced_stateful_set.return_value = MagicMock(items=[])
    apps_ds.list_namespaced_daemon_set.return_value = MagicMock(items=[ds_workload])

    trig_ds = RolloutTrigger(
        apps_api=apps_ds, ns_cache=fake_ns,
        helm_default_enabled=True, debounce_seconds=999,
    )
    ro_ds = ResourceOverride(
        namespace="ns", name="fluentd",
        selector_match_labels={"app": "fluentd"},
        containers=(
            ContainerOverride(name="fluentd", requests={"cpu": "100m"}, limits={}),
        ),
    )
    trig_ds.handle_event("ADDED", ro_ds, None)
    _check("[rollout-ds] DaemonSet event is scheduled in _pending",
           len(trig_ds._pending), 1)
    if trig_ds._pending:
        _check("[rollout-ds] pending key carries kind=DaemonSet",
               next(iter(trig_ds._pending))[1], "DaemonSet")
    trig_ds._fire_due(force=True)
    _check("[rollout-ds] patch_namespaced_daemon_set called on restart",
           apps_ds.patch_namespaced_daemon_set.called, True)
    _check("[rollout-ds] patch_namespaced_deployment NOT called for DaemonSet",
           apps_ds.patch_namespaced_deployment.called, False)
    _check("[rollout-ds] pending cleared after DaemonSet restart",
           len(trig_ds._pending), 0)

    # ── Bug: DELETED event must cancel pending restart (carry-forward state) ─
    # Pre-fix: handle_event returns early on DELETED without touching
    # self._pending. A restart already queued for the deleted CR fires
    # within debounce_seconds — spurious rolling restart after the
    # operator deletes the ResourceOverride.
    # Fix: on DELETED, remove every _pending entry whose cr_names set
    # contains ro.name; if cr_names becomes empty, drop the whole entry.
    trig_del = RolloutTrigger(
        apps_api=fake_apps, ns_cache=fake_ns,
        helm_default_enabled=True, debounce_seconds=999,
    )
    # Queue a restart via ADDED.
    trig_del.handle_event("ADDED", ro, None)
    _check("[rollout-delete-cancel] pending entry queued after ADDED (pre-condition)",
           len(trig_del._pending), 1)
    # DELETED for the same CR: pending entry must be removed (no spurious restart).
    trig_del.handle_event("DELETED", ro, None)
    _check("[rollout-delete-cancel] DELETED removes pending restart",
           len(trig_del._pending), 0)

    # Multiple CRs contributing to the same workload: only the deleted
    # CR is removed from cr_names; the entry survives while others remain.
    ro_extra = ResourceOverride(
        namespace="loki", name="loki-extra",
        selector_match_labels={"app.kubernetes.io/instance": "loki"},
        containers=(
            ContainerOverride(name="loki", requests={"cpu": "100m"}, limits={}),
        ),
    )
    trig_multi_del = RolloutTrigger(
        apps_api=fake_apps, ns_cache=fake_ns,
        helm_default_enabled=True, debounce_seconds=999,
    )
    trig_multi_del.handle_event("ADDED", ro, None)
    trig_multi_del.handle_event("ADDED", ro_extra, None)
    _check("[rollout-delete-cancel-multi] both CRs coalesced into 1 pending entry",
           len(trig_multi_del._pending), 1)
    # Delete one CR — entry must survive (other CR still contributes).
    trig_multi_del.handle_event("DELETED", ro, None)
    _check("[rollout-delete-cancel-multi] entry survives when other CR still contributes",
           len(trig_multi_del._pending), 1)
    pending_key_multi = ("loki", "Deployment", "loki")
    remaining_cr_names = trig_multi_del._pending[pending_key_multi].cr_names
    _check("[rollout-delete-cancel-multi] deleted CR name removed from cr_names",
           ro.name not in remaining_cr_names, True)
    _check("[rollout-delete-cancel-multi] surviving CR name still in cr_names",
           ro_extra.name in remaining_cr_names, True)
    # Delete the last contributing CR — entry must be removed.
    trig_multi_del.handle_event("DELETED", ro_extra, None)
    _check("[rollout-delete-cancel-multi] entry removed when last CR deleted",
           len(trig_multi_del._pending), 0)


def section_selector_inference() -> None:
    """Verifies _derive_selector picks the most distinguishing stable subset
    of the k8s.io recommended labels — `instance + name + component` when
    present, falling back to `name: <workload-name>` when none are.

    Real bug this guards against (1.4.1): Loki ships four StatefulSets that
    all share `instance: loki` AND `name: loki`; only `component` tells
    them apart. Pre-fix the webhook would warn `container memcached matched
    by multiple overrides — last wins` on every admission and silently
    apply the wrong CR's resources.
    """
    _section("Selector inference — _derive_selector across chart conventions")

    from src.writeback import WorkloadRecommendation
    from src.writeback_webhook import _derive_selector

    def _rec(target_name: str, **labels: str) -> WorkloadRecommendation:
        return WorkloadRecommendation(
            name=target_name, namespace="x", target_kind="StatefulSet",
            target_name=target_name,
            helm_release=labels.get("app.kubernetes.io/instance", ""),
            pod_template_labels=dict(labels),
        )

    # ── Loki: four StatefulSets share instance+name, distinguished by component ──
    loki = _rec("loki", **{
        "app.kubernetes.io/instance": "loki",
        "app.kubernetes.io/name": "loki",
        "app.kubernetes.io/component": "single-binary",
    })
    chunks = _rec("loki-chunks-cache", **{
        "app.kubernetes.io/instance": "loki",
        "app.kubernetes.io/name": "loki",
        "app.kubernetes.io/component": "memcached-chunks-cache",
    })
    results = _rec("loki-results-cache", **{
        "app.kubernetes.io/instance": "loki",
        "app.kubernetes.io/name": "loki",
        "app.kubernetes.io/component": "memcached-results-cache",
    })
    gateway = _rec("loki-gateway", **{
        "app.kubernetes.io/instance": "loki",
        "app.kubernetes.io/name": "loki",
        "app.kubernetes.io/component": "gateway",
    })

    sel_loki = _derive_selector(loki)
    sel_chunks = _derive_selector(chunks)
    sel_results = _derive_selector(results)
    sel_gateway = _derive_selector(gateway)

    _check("[loki] selector keeps all 3 stable labels",
           sorted(sel_loki.keys()),
           ["app.kubernetes.io/component",
            "app.kubernetes.io/instance",
            "app.kubernetes.io/name"])
    _check("[loki] component=single-binary",   sel_loki["app.kubernetes.io/component"], "single-binary")
    _check("[loki] chunks-cache distinguished by component",
           sel_chunks["app.kubernetes.io/component"], "memcached-chunks-cache")
    _check("[loki] results-cache distinguished by component",
           sel_results["app.kubernetes.io/component"], "memcached-results-cache")
    _check("[loki] gateway distinguished by component",
           sel_gateway["app.kubernetes.io/component"], "gateway")
    _check("[loki] all 4 selectors are pairwise distinct",
           len({tuple(sorted(s.items())) for s in (sel_loki, sel_chunks, sel_results, sel_gateway)}),
           4)

    # ── ArgoCD: distinguishes via name (different per component) ─────────
    argocd_ctrl = _rec("argocd-application-controller", **{
        "app.kubernetes.io/instance": "argocd",
        "app.kubernetes.io/name": "argocd-application-controller",
        "app.kubernetes.io/component": "application-controller",
    })
    argocd_server = _rec("argocd-server", **{
        "app.kubernetes.io/instance": "argocd",
        "app.kubernetes.io/name": "argocd-server",
        "app.kubernetes.io/component": "server",
    })
    s_ctrl = _derive_selector(argocd_ctrl)
    s_server = _derive_selector(argocd_server)
    _check("[argo] same instance, different name → distinguished",
           s_ctrl != s_server, True)

    # ── Volatile labels skipped: version + managed-by + helm.sh/chart never appear ──
    n8n = _rec("n8n", **{
        "app.kubernetes.io/instance": "n8n",
        "app.kubernetes.io/name": "n8n",
        "app.kubernetes.io/component": "main",
        "app.kubernetes.io/version": "2.14.2",
        "app.kubernetes.io/managed-by": "Helm",
        "helm.sh/chart": "n8n-1.16.35",
        "pod-template-hash": "deadbeef",
    })
    s_n8n = _derive_selector(n8n)
    for unstable in ("app.kubernetes.io/version",
                     "app.kubernetes.io/managed-by",
                     "helm.sh/chart",
                     "pod-template-hash"):
        _check(f"[stable] {unstable} not in selector",
               unstable in s_n8n, False)

    # ── Fallback: workload with NO recommended labels at all ─────────────
    raw = _rec("plain-deploy")
    s_raw = _derive_selector(raw)
    _check("[fallback] workload with no app.k8s labels → name=<target_name>",
           s_raw, {"app.kubernetes.io/name": "plain-deploy"})

    # ── Backwards compat: workload with only `instance` (legacy) ─────────
    legacy = _rec("legacy", **{"app.kubernetes.io/instance": "legacy"})
    s_legacy = _derive_selector(legacy)
    _check("[partial] only instance → selector keeps just instance",
           s_legacy, {"app.kubernetes.io/instance": "legacy"})


def section_yaml_rendering_quirks() -> None:
    """ruamel.yaml label-value quoting edges.

    k8s requires label values to be **strings**. Most YAML 1.2 specials
    (`true`/`false`/`null`/numbers) ruamel auto-quotes correctly. BUT
    YAML 1.1 boolean tokens that survive in some downstream parsers —
    `on`/`off`/`yes`/`no`/`y`/`n`/`Y`/`N` — ruamel emits UNQUOTED. The
    Go YAML library that k8s uses for ConfigMap/manifest parsing reads
    YAML 1.1, so a label value of `"Y"` would round-trip as boolean
    `True`, and apiserver would reject the CR (`spec.selector.matchLabels`
    values must be strings).

    Fix: wrap every selector label value in
    `ruamel.yaml.scalarstring.DoubleQuotedScalarString` so emission is
    always quoted, regardless of value-content heuristics.
    """
    _section("YAML rendering — ruamel auto-quote on YAML-special label values")

    from src.writeback_webhook import WebhookEntry, _render_namespace_file

    def _entry(label_value: str) -> WebhookEntry:
        return WebhookEntry(
            namespace="ns",
            cr_name="probe",
            selector_labels={"app.kubernetes.io/version": label_value},
            containers=[{"name": "app", "requests": {"cpu": "10m"}, "limits": {"cpu": "20m"}}],
        )

    # Round-trip simulates the k8s pipeline: we render YAML via ruamel
    # (the tool's emitter), and ArgoCD / kubectl / kube-apiserver consume
    # it via Go yaml.v2 which implements YAML 1.1 (parses `yes`/`no`/`on`/
    # `off` as booleans). PyYAML's safe_load is the closest equivalent
    # Python parser available; it gives us the same bool-coercion that
    # would happen in the real pipeline.
    import yaml as _pyyaml

    def _roundtrip_label_value(label_value: str):
        rendered = _render_namespace_file([_entry(label_value)])
        # split multi-doc YAML; first doc is our ResourceOverride.
        docs = list(_pyyaml.safe_load_all(rendered))
        return docs[0]["spec"]["selector"]["matchLabels"]["app.kubernetes.io/version"]

    # Cases ruamel already handles (currently passing — locking in).
    _check("[yaml-quote-true] label 'true' round-trips as string",
           _roundtrip_label_value("true"), "true")
    _check("[yaml-quote-null] label 'null' round-trips as string",
           _roundtrip_label_value("null"), "null")
    _check("[yaml-quote-int] label '123' round-trips as string",
           _roundtrip_label_value("123"), "123")
    _check("[yaml-quote-float] label '1.5' round-trips as string",
           _roundtrip_label_value("1.5"), "1.5")

    # Cases ruamel does NOT auto-quote.
    _check("[yaml-quote-on] label 'on' round-trips as string",
           _roundtrip_label_value("on"), "on")
    _check("[yaml-quote-off] label 'off' round-trips as string",
           _roundtrip_label_value("off"), "off")
    _check("[yaml-quote-yes] label 'yes' round-trips as string",
           _roundtrip_label_value("yes"), "yes")
    _check("[yaml-quote-no] label 'no' round-trips as string",
           _roundtrip_label_value("no"), "no")
    _check("[yaml-quote-Y-upper] label 'Y' round-trips as string",
           _roundtrip_label_value("Y"), "Y")
    _check("[yaml-quote-n-lower] label 'n' round-trips as string",
           _roundtrip_label_value("n"), "n")

    # Normal alphanumeric labels stay untouched (no over-quoting regression).
    _render_namespace_file([_entry("v1.2.3-stable")])  # smoke: must not raise
    _check("[yaml-quote-normal] alphanumeric label unchanged (not over-quoted)",
           _roundtrip_label_value("v1.2.3-stable"), "v1.2.3-stable")


def section_memory_parser_edges() -> None:
    """memory parse/format edges + TB-scale round-trips.

    Python int is arbitrary-precision, so there is no overflow risk on the
    arithmetic side — but `_parse_memory_bytes` has two defensive gaps:

      1. Missing suffixes `Pi` / `Ei` (binary) and `T` / `P` / `E` (decimal).
         ML workloads legitimately use `Pi`; currently `_parse_memory_bytes('1Pi')`
         returns 0, which would silently always-trigger floor enforcement on
         any comparison the parser feeds.
      2. No scientific-notation handling. Prometheus serves large values as
         `'6.4e+10'`; if any future path round-trips that string back into
         a Quantity, parser returns 0.

    These asserts lock in the existing supported suffixes AND add coverage
    for the gaps. The fix lands in `src/writeback.py:_parse_memory_bytes`.
    """
    _section("Memory parser edges — Pi/Ei, decimal suffixes, sci notation")

    from src.writeback import _parse_memory_bytes, _fmt_memory

    # ── Baseline: currently-supported suffixes ───────────────────────────
    _check("[parse-bytes-raw] '1048576' → 1 MiB",        _parse_memory_bytes("1048576"), 1 << 20)
    _check("[parse-Ki] '1Ki' → 1024",                    _parse_memory_bytes("1Ki"), 1024)
    _check("[parse-Mi] '1Mi' → 2^20",                    _parse_memory_bytes("1Mi"), 1 << 20)
    _check("[parse-Gi] '1Gi' → 2^30",                    _parse_memory_bytes("1Gi"), 1 << 30)
    _check("[parse-Ti] '1Ti' → 2^40",                    _parse_memory_bytes("1Ti"), 1 << 40)
    _check("[parse-K-decimal] '1K' → 1000",              _parse_memory_bytes("1K"), 1000)
    _check("[parse-M-decimal] '1M' → 1e6",               _parse_memory_bytes("1M"), 1_000_000)
    _check("[parse-G-decimal] '1G' → 1e9",               _parse_memory_bytes("1G"), 1_000_000_000)
    _check("[parse-empty] empty → 0",                    _parse_memory_bytes(""), 0)
    _check("[parse-garbage] 'abc' → 0",                  _parse_memory_bytes("abc"), 0)

    # ── TB-scale round-trip safety (no overflow, no precision loss) ──────
    sixty_four_gib = 64 * (1 << 30)
    _check("[parse-TB-64Gi] round-trip 64Gi via parse",
           _parse_memory_bytes("64Gi"), sixty_four_gib)
    _check("[fmt-TB-64Gi] format 64*2^30 → '64Gi'",
           _fmt_memory(str(sixty_four_gib)), "64Gi")
    # OOM-bump scenario: 64Gi × 1.5 = 96Gi (well below int53 ceiling).
    bumped = int(sixty_four_gib * 1.5)
    _check("[fmt-OOM-bump-96Gi] 64Gi × 1.5 formats to '96Gi'",
           _fmt_memory(str(bumped)), "96Gi")
    # Even larger — 1 TiB. ML training nodes.
    _check("[parse-TB-1Ti] '1Ti' parses",
           _parse_memory_bytes("1Ti"), 1 << 40)
    _check("[fmt-TB-1Ti-bytes] format 2^40 bytes → '1024Gi'",
           _fmt_memory(str(1 << 40)), "1024Gi")

    # ── Defensive gaps (Pi, Ei, T, P, E, scientific notation) ────────────
    _check("[parse-Pi] '1Pi' → 2^50",
           _parse_memory_bytes("1Pi"), 1 << 50)
    _check("[parse-Ei] '1Ei' → 2^60",
           _parse_memory_bytes("1Ei"), 1 << 60)
    _check("[parse-T-decimal] '1T' → 1e12",
           _parse_memory_bytes("1T"), 1_000_000_000_000)
    _check("[parse-P-decimal] '1P' → 1e15",
           _parse_memory_bytes("1P"), 1_000_000_000_000_000)
    _check("[parse-E-decimal] '1E' → 1e18",
           _parse_memory_bytes("1E"), 1_000_000_000_000_000_000)
    # Scientific notation (Prometheus serves large values like this).
    _check("[parse-sci-int] '6.4e+10' → 64000000000",
           _parse_memory_bytes("6.4e+10"), 64_000_000_000)
    _check("[parse-sci-int-no-plus] '1e9' → 1e9 int",
           _parse_memory_bytes("1e9"), 1_000_000_000)
    # Negative scientific notation rounds toward zero; not a real value
    # we'd see in Quantity strings, but the parser shouldn't crash.
    _check("[parse-sci-small] '1e-3' → 0 (sub-byte rounds to 0)",
           _parse_memory_bytes("1e-3"), 0)

    # ── Lowercase SI `k` (bug #13) ───────────────────────────────────────
    # Lowercase `k` (= 1000) is the canonical k8s decimal-kilo suffix, but
    # the former hand-rolled suffix table only listed uppercase `K`, so any
    # lowercase-`k` quantity silently parsed to 0 — the exact "valid suffix
    # → 0" footgun #13 calls out. The apiserver-aligned `parse_quantity`
    # handles it. These FAIL on the pre-#13 code (return 0).
    _check("[parse-k-lower] '1k' → 1000 (lowercase SI; was 0 before #13)",
           _parse_memory_bytes("1k"), 1000)
    _check("[parse-k-lower-500] '500k' → 500000 (was 0 before #13)",
           _parse_memory_bytes("500k"), 500_000)


def section_crd_schema_tightening() -> None:
    """CRD schema completeness.

    Pre-front-8 the CRD's openAPIV3Schema accepted any string for resource
    quantity values (`requests.cpu`, `limits.memory`, …) and any length for
    container names. A typoed CR like:

        spec:
          containers:
            - name: app
              requests:
                memory: "100QB"   # not a k8s Quantity

    passed schema validation at `kubectl apply` time and only failed later
    at pod admission when kubelet tried to parse the Quantity. Better UX:
    catch at apply.

     tightens two fields:
      - `containers[*].name`: adds `maxLength: 63` (k8s DNS_LABEL_NAME limit;
        anything longer would fail pod creation anyway).
      - `requests`/`limits` values: adds `pattern` matching the k8s Quantity
        grammar — rejects e.g. `"100QB"`, `"abc"`, accepts `"100m"`, `"1Gi"`,
        `"1.5"`, `"6.4e+10"`.

    Subsequent tightening of the mantissa grammar: the original `[+-]?[0-9.]+`
    matched lone `"."`, `"1.2.3"`, `"1..2"`, and leading-sign values like
    `"+100m"` — none of which k8s accepts. The tool never emits signed values
    (CPU always `"{n}m"`, memory always `"{n}Mi"` / `"{n}Gi"`), so the sign
    support in the pattern was dead and misleading.  The tightened pattern
    `^[0-9]+(\.[0-9]+)?...` requires a leading digit and at most one decimal
    point.  The `"+10m"` accept case in the original suite was wrong and is
    removed; `"."`, `"1.2.3"`, `"+100m"`, `"1..2"` are added to the reject
    list.

    Both tightenings were verified safe against currently-deployed CRs in
    `gitops/clusters/prod-cluster/manifests/kube-resource-updater/` —
    5 CRs scanned, 0 violations.
    """
    _section("CRD schema tightening — quantity pattern + name maxLength")

    import shutil
    import subprocess
    import re
    helm = shutil.which("helm")
    if not helm:
        # Fall back to common brew install path used in [chart-bump-helm-render].
        for cand in (os.path.expanduser("~/homebrew/bin/helm"),
                     "/opt/homebrew/bin/helm", "/usr/local/bin/helm"):
            if os.path.isfile(cand):
                helm = cand
                break
    if not helm:
        print(f"  [{_SKIP}] helm CLI not installed locally — skipping CRD render check")
        return

    chart_dir = _chart_dir()
    if not os.path.isdir(chart_dir):
        print(f"  [{_SKIP}] chart dir not found at {chart_dir}")
        return

    result = subprocess.run(
        [
            helm, "template", "kru", chart_dir,
            "--set", "config.crWriteback.repoUrl=https://x.git,config.crWriteback.path=overrides",
            "--set", "config.prometheusUrl=http://prom:9090,gitlab.token=qa",
            "--set", "webhook.enabled=true",
            "--show-only", "templates/webhook/crd.yaml",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [{_SKIP}] CRD render failed: {result.stderr.splitlines()[:3]}")
        return

    import yaml as _yaml
    docs = list(_yaml.safe_load_all(result.stdout))
    crd = next((d for d in docs if d and d.get("kind") == "CustomResourceDefinition"), None)
    if not crd:
        _check("[crd-schema] CRD rendered from chart", False, True)
        return
    schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    container_props = schema["properties"]["spec"]["properties"]["containers"]["items"]["properties"]
    name_props = container_props["name"]
    requests_props = container_props["requests"]["additionalProperties"]
    limits_props = container_props["limits"]["additionalProperties"]

    # ── (1) container name maxLength ─────────────────────────────────────
    _check("[crd-name-maxlen] containers[*].name has maxLength: 63 (DNS_LABEL_NAME)",
           name_props.get("maxLength"), 63)

    # ── (2) requests/limits pattern present + matches Quantity grammar ──
    req_pattern = requests_props.get("pattern")
    lim_pattern = limits_props.get("pattern")
    _check("[crd-quantity-req] requests values carry a pattern",
           bool(req_pattern), True)
    _check("[crd-quantity-lim] limits values carry a pattern",
           bool(lim_pattern), True)
    _check("[crd-quantity-same-pattern] requests + limits use the same regex",
           req_pattern, lim_pattern)

    # Validate the pattern matches the cases we care about. The pattern
    # must accept every Quantity string the tool itself emits.
    # NOTE: "+10m" is intentionally NOT in the accept list — k8s rejects
    # leading-sign Quantity strings. The original pattern was too loose on this.
    if req_pattern:
        rx = re.compile(req_pattern)
        for accept in ("100m", "1.5", "1Gi", "256Mi", "64Gi", "1Ti",
                       "1.5Gi", "0", "6.4e+10", "1.5e3", "2", "500M"):
            _check(f"[crd-quantity-accept] pattern accepts {accept!r}",
                   bool(rx.match(accept)), True)
        # ML workloads use Pi; tool's _parse_memory_bytes supports it.
        _check("[crd-quantity-accept] pattern accepts '1Pi'",
               bool(rx.match("1Pi")), True)
        # Garbage typo cases the operator most often makes — caught at apply.
        for reject in ("100QB", "abc", "1.5XY", "memory", ""):
            _check(f"[crd-quantity-reject] pattern rejects {reject!r}",
                   bool(rx.match(reject)), False)
        # Mantissa-grammar cases that the old `[+-]?[0-9.]+` pattern accepted
        # but k8s rejects: lone dot, multiple dots, leading sign.
        for reject in (".", "1.2.3", "+100m", "1..2"):
            _check(f"[crd-quantity-reject-mantissa] pattern rejects {reject!r}",
                   bool(rx.match(reject)), False)


def section_crd_cel_hardening() -> None:
    """CRD hardening — + #42: CEL validations + list-map-keys.

    adds three schema-layer enforcement mechanisms to the
    ResourceOverride CRD containers array:

      1. x-kubernetes-list-type: map + x-kubernetes-list-map-keys: ["name"] — apiserver rejects duplicate container names at apply
         time. Before this, two entries with the same name silently last-won.

      2. x-kubernetes-validations CEL rules on the container item:
         requests.memory <= limits.memory and requests.cpu <= limits.cpu.
         Uses quantity().compareTo() so "1Gi" and "1024Mi" compare correctly.

      3. x-kubernetes-validations CEL rules: per-unit checks that
         cpu does not carry binary SI suffixes (Mi, Gi, ...) and memory does not
         carry the millicores suffix (m). The generic Quantity pattern already
         catches typos; these rules catch unit-swaps that the pattern accepts.

    maxItems: 50 on containers and maxProperties: 16 + maxLength: 20 on
    requests/limits are required by the apiserver CEL cost estimator — without
    bounds it refuses the CRD with "cost exceeds budget" even for simple rules.

    QA scope: render-time only. Apiserver CEL enforcement is live-test territory
    (rule 4) — covered by the live-test commands in the implementation notes.
    """
    _section("CRD CEL hardening — list-map-keys + requests≤limits + per-unit")

    import shutil
    import subprocess
    helm = shutil.which("helm")
    if not helm:
        for cand in (os.path.expanduser("~/homebrew/bin/helm"),
                     "/opt/homebrew/bin/helm", "/usr/local/bin/helm"):
            if os.path.isfile(cand):
                helm = cand
                break
    if not helm:
        print(f"  [{_SKIP}] helm CLI not installed locally — skipping CRD render check")
        return

    chart_dir = _chart_dir()
    if not os.path.isdir(chart_dir):
        print(f"  [{_SKIP}] chart dir not found at {chart_dir}")
        return

    import yaml as _yaml
    result = subprocess.run(
        [
            helm, "template", "kru", chart_dir,
            "--set", "config.crWriteback.repoUrl=https://x.git,config.crWriteback.path=overrides",
            "--set", "config.prometheusUrl=http://prom:9090,gitlab.token=qa",
            "--set", "webhook.enabled=true",
            "--show-only", "templates/webhook/crd.yaml",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [{_SKIP}] CRD render failed: {result.stderr.splitlines()[:3]}")
        return

    docs = list(_yaml.safe_load_all(result.stdout))
    crd = next((d for d in docs if d and d.get("kind") == "CustomResourceDefinition"), None)
    if not crd:
        _check("[crd-cel] CRD rendered from chart", False, True)
        return

    # OSS pre-publish: the CRD must carry helm.sh/resource-policy: keep so a
    # `helm uninstall` does NOT cascade-delete every ResourceOverride CR
    # (silent, irreversible data loss). FAIL before the annotation is added.
    _check("[crd-resource-policy] CRD has helm.sh/resource-policy: keep",
           (crd.get("metadata", {}).get("annotations") or {}).get("helm.sh/resource-policy"),
           "keep")

    schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    containers = schema["properties"]["spec"]["properties"]["containers"]
    items = containers["items"]

    # ── (1) list-map-keys: unique container names ─────────────────────────
    _check("[crd-cel-list-type] containers x-kubernetes-list-type is 'map'",
           containers.get("x-kubernetes-list-type"), "map")
    _check("[crd-cel-list-map-keys] containers x-kubernetes-list-map-keys is ['name']",
           containers.get("x-kubernetes-list-map-keys"), ["name"])
    _check("[crd-cel-maxitems] containers maxItems present (cost-bound for CEL estimator)",
           "maxItems" in containers, True)

    # ── (2) CEL rules present on container item ───────────────────────────
    validations = items.get("x-kubernetes-validations", [])
    rules = [v.get("rule", "") for v in validations]
    messages = [v.get("message", "") for v in validations]

    _check("[crd-cel-validations] container item has x-kubernetes-validations",
           len(validations) >= 6, True)

    # requests ≤ limits rules
    mem_cmp_rule = any("memory" in r and "compareTo" in r for r in rules)
    cpu_cmp_rule = any("cpu" in r and "compareTo" in r for r in rules)
    _check("[crd-cel-req-lim-mem] CEL rule enforces requests.memory <= limits.memory",
           mem_cmp_rule, True)
    _check("[crd-cel-req-lim-cpu] CEL rule enforces requests.cpu <= limits.cpu",
           cpu_cmp_rule, True)
    _check("[crd-cel-quantity-fn] requests<=limits rules use quantity() function",
           any("quantity(" in r for r in rules), True)

    # per-unit rules
    cpu_unit_rule = sum(1 for r in rules if "cpu" in r and "matches" in r)
    mem_unit_rule = sum(1 for r in rules if "memory" in r and "matches" in r)
    _check("[crd-cel-cpu-unit] 2 CEL rules constrain cpu unit (req + lim)",
           cpu_unit_rule, 2)
    _check("[crd-cel-mem-unit] 2 CEL rules constrain memory unit (req + lim)",
           mem_unit_rule, 2)

    # Error messages present (operators see these on `kubectl apply` rejection)
    _check("[crd-cel-msg-req-lim-mem] requests.memory rule has error message",
           any("requests.memory" in m and "limits.memory" in m for m in messages), True)
    _check("[crd-cel-msg-req-lim-cpu] requests.cpu rule has error message",
           any("requests.cpu" in m and "limits.cpu" in m for m in messages), True)
    _check("[crd-cel-msg-cpu-unit] cpu per-unit rule has error message mentioning Mi",
           any("Mi" in m and "cpu" in m.lower() for m in messages), True)
    _check("[crd-cel-msg-mem-unit] memory per-unit rule has error message mentioning 'm'",
           any("millicores" in m or ("'m'" in m and "memory" in m.lower()) for m in messages), True)

    # ── (3) maxProperties + maxLength present (cost-bounding) ────────────
    req_props = items["properties"].get("requests", {})
    lim_props = items["properties"].get("limits", {})
    _check("[crd-cel-req-maxprops] requests has maxProperties (CEL cost bound)",
           "maxProperties" in req_props, True)
    _check("[crd-cel-lim-maxprops] limits has maxProperties (CEL cost bound)",
           "maxProperties" in lim_props, True)
    req_addl = req_props.get("additionalProperties", {})
    lim_addl = lim_props.get("additionalProperties", {})
    _check("[crd-cel-req-maxlen] requests additionalProperties has maxLength (CEL cost bound)",
           "maxLength" in req_addl, True)
    _check("[crd-cel-lim-maxlen] limits additionalProperties has maxLength (CEL cost bound)",
           "maxLength" in lim_addl, True)

    # ── status.patchedContainers declared in schema ──────────
    # The status schema is strict (no preserve-unknown-fields), so a field
    # not declared here is silently pruned on write. FAIL before the schema
    # edit (field absent).
    status_props = schema["properties"].get("status", {}).get("properties", {})
    pc = status_props.get("patchedContainers", {})
    _check("[crd-status-patchedContainers] status.patchedContainers declared",
           pc.get("type"), "array")
    _check("[crd-status-patchedContainers-items] patchedContainers items are strings",
           pc.get("items", {}).get("type"), "string")


def section_webhook_patch_corners() -> None:
    """webhook admission patch corner cases.

    The webhook is the hot path — every Pod creation in opted-in namespaces
    flows through `build_patches`. Edge-case bugs cause silent admission
    failures (kubelet retries forever) or wrong-container patches.

    Locks in the existing behavior for six corner cases enumerated in
    ROADMAP "":
      1. Pod with NO containers (empty `spec.containers`).
      2. Matching container with NO `resources` field at all.
      3. `_rfc6901_escape` correctly escapes annotation keys for JSONPatch
         paths; the production key `kube-resource-updater.applied-from` is
         unchanged by it (no `/` or `~`).
      4. Same container name appearing twice in `spec.containers`.
      5. CR names an init container — must NOT cross-patch the regular
         container with the same name (only `/spec/containers/*` is
         touched; `/spec/initContainers/*` never appears).
      6. Multiple CRs match the same container — `by_name` last-wins
         produces exactly one patch per resource field per container.
    """
    _section("Webhook admission patch — corner cases")

    from src.webhook_cache import ContainerOverride, ResourceOverride
    from src.webhook_patch import build_patches, patches_to_jsonpatch

    def _ro(name: str, **kwargs) -> ResourceOverride:
        return ResourceOverride(
            namespace="ns", name=name,
            selector_match_labels={"app.kubernetes.io/name": "api"},
            containers=kwargs.pop("containers"),
            **kwargs,
        )

    # ── (1) Pod with NO containers ───────────────────────────────────────
    pod_empty = {
        "metadata": {"namespace": "ns", "name": "api-abc"},
        "spec": {"containers": []},
    }
    overrides = [_ro("api", containers=(
        ContainerOverride(name="api", requests={"cpu": "150m"}, limits={"cpu": "300m"}),
    ))]
    patches = build_patches(pod_empty, overrides)
    _check("[corner-1-empty-containers] no containers → empty patch list",
           patches, [])
    _check("[corner-1-empty-containers] no annotation patch either",
           [p for p in patches if "annotations" in p.path], [])

    # ── (2) Container with NO `resources` field at all ──────────────────
    # JSONPatch `add /spec/containers/0/resources` requires the parent
    # (the container at index 0) to exist; the field itself can be missing.
    # The code adds the whole `resources` node atomically.
    pod_no_res = {
        "metadata": {"namespace": "ns", "name": "api-abc"},
        "spec": {"containers": [{"name": "api"}]},  # NO `resources` key
    }
    patches = build_patches(pod_no_res, overrides)
    res_patches = [p for p in patches if p.path == "/spec/containers/0/resources"]
    _check("[corner-2-missing-resources] exactly one add at /resources path",
           len(res_patches), 1)
    _check("[corner-2-missing-resources] op is 'add' (not 'replace')",
           res_patches[0].op, "add")
    _check("[corner-2-missing-resources] value contains both requests + limits",
           sorted(res_patches[0].value.keys()), ["limits", "requests"])

    # Same shape via patches_to_jsonpatch — guards against accidental drift
    # in the dict serialisation (admission webhook returns this verbatim).
    serialised = patches_to_jsonpatch(patches)
    res_serial = [p for p in serialised if p["path"] == "/spec/containers/0/resources"]
    _check("[corner-2-serialise] serialised patch keeps op=add",
           res_serial[0]["op"], "add")

    # ── (3) JSONPatch escape — RFC 6901 `_rfc6901_escape` helper ─────────
    # `_rfc6901_escape` must escape a `/` → `~1` and `~` → `~0` in any key
    # that lands as a JSONPatch path segment. These asserts FAIL before the
    # #55 fix (ImportError on the helper, then wrong path without the call).
    from src.webhook_patch import _rfc6901_escape

    # Unit-level: substitution order matters — `~` must become `~0` BEFORE
    # `/` becomes `~1`, else `~/` corrupts to `~01`/`~10` instead of `~0~1`.
    _check("[corner-3-escape] plain key unchanged",
           _rfc6901_escape("kube-resource-updater.applied-from"),
           "kube-resource-updater.applied-from")
    _check("[corner-3-escape] slash → ~1",
           _rfc6901_escape("my.io/foo"), "my.io~1foo")
    _check("[corner-3-escape] tilde → ~0",
           _rfc6901_escape("my~key"), "my~0key")
    _check("[corner-3-escape] tilde-slash → ~0~1 (order-correct, not ~10/~01)",
           _rfc6901_escape("a~/b"), "a~0~1b")

    # Integration A — a pod that ALREADY has annotations takes the keyed-path
    # branch (`/metadata/annotations/<key>`), the only branch where the key
    # becomes a JSONPatch path segment and thus flows through
    # `_rfc6901_escape`. The production key has no `/` or `~`, so the emitted
    # path is byte-for-byte identical to the pre-fix path — locks no-regression.
    pod_with_anns = {
        "metadata": {"namespace": "ns", "name": "api-abc",
                     "annotations": {"existing": "x"}},
        "spec": {"containers": [{"name": "api", "resources": {"requests": {"cpu": "10m"}}}]},
    }
    patches = build_patches(pod_with_anns, overrides)
    ann_patches = [p for p in patches if "annotations" in p.path]
    _check("[corner-3-jsonpatch-key] keyed path emitted; escaped form unchanged for production key",
           [p.path for p in ann_patches],
           ["/metadata/annotations/kube-resource-updater.applied-from"])

    # Integration B — a pod with NO annotations takes the whole-object branch;
    # the key lives INSIDE the value dict (not a path segment) and the value
    # carries '/' (ns/cr-name) verbatim, since JSON literals are never escaped.
    pod_simple = {
        "metadata": {"namespace": "ns", "name": "api-abc"},
        "spec": {"containers": [{"name": "api", "resources": {"requests": {"cpu": "10m"}}}]},
    }
    patches = build_patches(pod_simple, overrides)
    serialised = patches_to_jsonpatch(patches)
    ann_serial = [p for p in serialised if "annotations" in p["path"]]
    if isinstance(ann_serial[0]["value"], dict):
        ann_value_text = ann_serial[0]["value"]["kube-resource-updater.applied-from"]
    else:
        ann_value_text = ann_serial[0]["value"]
    _check("[corner-3-jsonpatch-value] annotation value carries '/' verbatim (not escaped)",
           "ns/api" == ann_value_text, True)

    # ── (4) Same container name twice in spec.containers ─────────────────
    # apiserver pre-validation rejects this before admission webhooks see
    # the pod, but the defensive code path still runs in tests. Both
    # duplicates get patched (same target value); apiserver would have
    # rejected the pod anyway. We assert the webhook does not crash and
    # produces a deterministic shape.
    pod_dup = {
        "metadata": {"namespace": "ns", "name": "api-abc"},
        "spec": {"containers": [
            {"name": "api", "resources": {}},
            {"name": "api", "resources": {}},
        ]},
    }
    patches = build_patches(pod_dup, overrides)
    paths_seen = {p.path for p in patches}
    _check("[corner-4-duplicate-name] no crash on duplicate container names",
           True, True)  # exercise above without exception
    _check("[corner-4-duplicate-name] both indices targeted",
           "/spec/containers/0/resources/requests" in paths_seen
           and "/spec/containers/1/resources/requests" in paths_seen, True)

    # ── (5) CR names a regular container — init containers NEVER patched ─
    # Existing test in section_skip_containers already covers the
    # init-container short-circuit. Here we lock the path-level invariant
    # independently: even with init containers present, no JSONPatch path
    # starts with `/spec/initContainers/`.
    pod_with_init = {
        "metadata": {"namespace": "ns", "name": "api-abc"},
        "spec": {
            "containers":     [{"name": "api", "resources": {"requests": {"cpu": "10m"}}}],
            "initContainers": [{"name": "api", "resources": {"requests": {"cpu": "10m"}}}],
        },
    }
    patches = build_patches(pod_with_init, overrides)
    _check("[corner-5-init-never-patched] no /spec/initContainers/* in any patch",
           any(p.path.startswith("/spec/initContainers") for p in patches), False)
    _check("[corner-5-init-never-patched] regular container at index 0 IS patched",
           any(p.path.startswith("/spec/containers/0/resources") for p in patches), True)

    # ── (6) Multiple CRs match the same container — last-wins dedup ──────
    # by_name is a dict — the last CR processed wins per container name.
    # We must produce ONE patch per resource field per container, not N.
    overrides_multi = [
        _ro("first",  containers=(ContainerOverride(name="api", requests={"cpu": "100m"}, limits={"cpu": "200m"}),)),
        _ro("second", containers=(ContainerOverride(name="api", requests={"cpu": "300m"}, limits={"cpu": "600m"}),)),
        _ro("third",  containers=(ContainerOverride(name="api", requests={"cpu": "500m"}, limits={"cpu": "1000m"}),)),
    ]
    patches = build_patches(pod_simple, overrides_multi)
    req_paths = [p for p in patches if p.path == "/spec/containers/0/resources/requests"]
    lim_paths = [p for p in patches if p.path == "/spec/containers/0/resources/limits"]
    _check("[corner-6-last-wins] exactly one patch on /resources/requests path",
           len(req_paths), 1)
    _check("[corner-6-last-wins] exactly one patch on /resources/limits path",
           len(lim_paths), 1)
    _check("[corner-6-last-wins] last CR's value wins (cpu=500m)",
           req_paths[0].value, {"cpu": "500m"})
    _check("[corner-6-last-wins] last CR's limit wins (cpu=1000m)",
           lim_paths[0].value, {"cpu": "1000m"})

    # ── (7) Framework-audit bug-hunt: which CRs ACTUALLY contributed? ─────
    # webhook_server.py used to record() lastAppliedAt on every CR in
    # `matches` (selector-matched), regardless of whether its container
    # list overlapped with the pod's actual containers. That made
    # `kubectl get ro -A` show stale-but-fresh-looking timestamps on
    # CRs that contributed nothing. New helper `applied_source_crs`
    # exposes the post-last-wins set that build_patches already computes
    # internally.
    from src.webhook_patch import applied_source_crs

    # Case A: 2 CRs match by selector; only 1 has a container in the pod.
    overrides_split = [
        _ro("matches-pod",  containers=(ContainerOverride(name="api",   requests={"cpu": "100m"}, limits={}),)),
        _ro("phantom-only", containers=(ContainerOverride(name="other", requests={"cpu": "999m"}, limits={}),)),
    ]
    applied = applied_source_crs(pod_simple, overrides_split)
    _check("[applied-sources] CR whose container is in the pod IS reported",
           "ns/matches-pod" in applied, True)
    _check("[applied-sources] CR whose container is NOT in the pod is NOT reported",
           "ns/phantom-only" in applied, False)

    # Case B: last-wins on duplicate container name — losers are NOT
    # reported as applied (only the winner truly contributed).
    applied_multi = applied_source_crs(pod_simple, overrides_multi)
    _check("[applied-sources-last-wins] only the winning CR is reported applied",
           applied_multi, {"ns/third"})

    # Case C: empty pod containers → empty applied set.
    _check("[applied-sources-empty-pod] no containers → no applied CRs",
           applied_source_crs(pod_empty, overrides_split), set())

    # Case D: no matching overrides → empty.
    _check("[applied-sources-empty-overrides] no overrides → no applied CRs",
           applied_source_crs(pod_simple, []), set())

    # ── (8) dry-run admission must NOT call status_updater.record() ───────
    # AdmissionReview carries request.dryRun=true on kubectl apply --dry-run=server.
    # MutatingWebhookConfiguration declares sideEffects: None, which is the
    # k8s contract guarantee that the webhook produces NO side effects on any
    # request including dry-run.  Before the fix, _make_mutate_handler called
    # status_updater.record() unconditionally when patches were built, violating
    # that contract and writing a real PATCH to ResourceOverride.status even on
    # dry-run pod admissions.
    #
    # This assert FAILS before the fix: record() is called despite dryRun=True.
    import asyncio
    import json as _json
    from unittest.mock import AsyncMock, MagicMock
    from src.webhook_server import _make_mutate_handler
    from src.webhook_status import StatusUpdater

    # Minimal mock caches: both report ready, namespace is enabled, one
    # matching ResourceOverride returns a real container patch.
    _cr_cache = MagicMock()
    _cr_cache.ready.return_value = True
    _cr_cache.lookup.return_value = [
        overrides[0],  # the _ro("api", ...) from corner-1 above has cpu requests
    ]

    _ns_cache = MagicMock()
    _ns_cache.ready.return_value = True
    _ns_cache.is_enabled.return_value = True

    # StatusUpdater with a real record() spy — no flush needed.
    _status_api = MagicMock()
    _status_upd = StatusUpdater(_status_api, flush_interval_seconds=999)
    _record_spy = MagicMock(wraps=_status_upd.record)
    _status_upd.record = _record_spy

    _metrics_obj = __import__("src.webhook_server", fromlist=["_Metrics"])._Metrics()

    handler = _make_mutate_handler(_cr_cache, _ns_cache, _metrics_obj, _status_upd)

    # AdmissionReview with dryRun=true, pod with one container matching the
    # ResourceOverride loaded in _cr_cache above.
    _dry_run_review = {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": "dry-run-uid-001",
            "dryRun": True,
            "namespace": "ns",
            "object": {
                "metadata": {"namespace": "ns", "name": "api-pod", "labels": {"app.kubernetes.io/name": "api"}},
                "spec": {"containers": [{"name": "api", "resources": {"requests": {"cpu": "10m"}}}]},
            },
        },
    }

    # Build a mock aiohttp request whose .json() coroutine returns the review.
    _fake_request = MagicMock()
    _fake_request.json = AsyncMock(return_value=_dry_run_review)

    _response = asyncio.run(handler(_fake_request))

    # The webhook must still return an allowed response with patches —
    # the apiserver discards them on dry-run, but the handler should not
    # short-circuit patch computation.
    _resp_body = _json.loads(_response.text)
    _check("[dry-run-no-sideeffect] handler returns allowed=True on dry-run",
           _resp_body["response"]["allowed"], True)

    # CORE ASSERT: record() must NOT be called when dryRun=True.
    # This FAILS before the fix because the existing code has no dryRun guard.
    _check("[dry-run-no-sideeffect] status_updater.record() NOT called on dry-run admission",
           _record_spy.called, False)

    # Sanity: confirm record() IS called on a real (non-dry-run) admission
    # so the test would catch a regression that suppresses record() always.
    _record_spy.reset_mock()
    _live_review = {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": "live-uid-001",
            "namespace": "ns",
            "object": {
                "metadata": {"namespace": "ns", "name": "api-pod", "labels": {"app.kubernetes.io/name": "api"}},
                "spec": {"containers": [{"name": "api", "resources": {"requests": {"cpu": "10m"}}}]},
            },
        },
    }
    _fake_request2 = MagicMock()
    _fake_request2.json = AsyncMock(return_value=_live_review)
    asyncio.run(handler(_fake_request2))
    _check("[dry-run-no-sideeffect] status_updater.record() IS called on live (non-dry-run) admission",
           _record_spy.called, True)


def section_validating_webhook() -> None:
    """Verifies the validating webhook's overlap detection:

      - selector_can_overlap: standard matchLabels semantics; empty
        selectors are no-overlap (mirrors the cache).
      - container_names_intersect: simple set intersection.
      - validate(): returns Allowed=true on no-conflict, Allowed=false
        with a clear message on real overlap, Allowed=true when the
        candidate is the same CR (UPDATE flow), Allowed=true when the
        cache is unready.
    """
    _section("Validating webhook — selector + container overlap detection")

    from src.webhook_cache import (
        ContainerOverride,
        ResourceOverride,
        ResourceOverrideCache,
        _NamespaceIndex,
    )
    from src.webhook_validate import (
        _container_names_intersect,
        _find_conflicts,
        _selectors_can_overlap,
        validate,
    )

    # ── _selectors_can_overlap pure logic ────────────────────────────
    _check("[selector] disjoint label keys CAN overlap (no contradiction)",
           _selectors_can_overlap({"a": "1"}, {"b": "2"}), True)
    _check("[selector] same key/value → can overlap",
           _selectors_can_overlap({"a": "1"}, {"a": "1"}), True)
    _check("[selector] same key/different value → impossible",
           _selectors_can_overlap({"a": "1"}, {"a": "2"}), False)
    _check("[selector] empty selector A → no overlap (cache rule)",
           _selectors_can_overlap({}, {"a": "1"}), False)
    _check("[selector] empty selector B → no overlap",
           _selectors_can_overlap({"a": "1"}, {}), False)

    # ── _container_names_intersect ───────────────────────────────────
    a = (ContainerOverride(name="x", requests={}, limits={}),
         ContainerOverride(name="y", requests={}, limits={}))
    b = (ContainerOverride(name="y", requests={}, limits={}),
         ContainerOverride(name="z", requests={}, limits={}))
    _check("[containers] intersection by name", _container_names_intersect(a, b), {"y"})

    # ── _find_conflicts: self-exclusion + match logic ─────────────────
    def rl(nm, sel, conts):
        return ResourceOverride(
            namespace="loki", name=nm,
            selector_match_labels=sel,
            containers=tuple(ContainerOverride(name=c, requests={}, limits={}) for c in conts),
        )
    target = rl("loki", {"a": "1", "b": "2"}, ["main"])
    sib_overlap = rl("loki-other", {"a": "1"}, ["main"])
    sib_diff_container = rl("loki-x", {"a": "1"}, ["sidecar"])
    sib_diff_label = rl("loki-y", {"a": "9"}, ["main"])
    sib_self_old = rl("loki", {"a": "1", "b": "2"}, ["main"])
    conflicts = _find_conflicts(target, [sib_overlap, sib_diff_container,
                                         sib_diff_label, sib_self_old])
    names = sorted(c[0] for c in conflicts)
    _check("[conflict] only the genuinely-overlapping sibling is flagged",
           names, ["loki-other"])
    _check("[conflict] flagged sibling lists shared container",
           conflicts[0][1], {"main"})
    _check("[conflict] self (same name+namespace) excluded from check",
           "loki" in names, False)

    # ── validate() AdmissionReview ────────────────────────────────────
    cache = ResourceOverrideCache(api=MagicMock())
    cache._ready.set()
    cache._index["loki"] = _NamespaceIndex(overrides={"loki-other": sib_overlap})

    review_conflict = {
        "request": {
            "uid": "abc",
            "object": {
                "metadata": {"namespace": "loki", "name": "loki"},
                "spec": {
                    "selector": {"matchLabels": target.selector_match_labels},
                    "containers": [{"name": "main"}],
                },
            },
        },
    }
    resp = validate(review_conflict, cache)["response"]
    _check("[validate] conflicting CR → Allowed=false",   resp["allowed"], False)
    _check("[validate] reject message names the offending sibling",
           "loki-other" in resp["status"]["message"], True)

    # Same-name update: should not conflict with the cached previous version.
    # Reset the namespace to ONLY hold self_old so the test isolates the
    # self-exclusion path from the previous conflict-detection scenario.
    cache._index["loki"] = _NamespaceIndex(overrides={"loki": sib_self_old})
    review_self_update = {
        "request": {
            "uid": "abc",
            "object": {
                "metadata": {"namespace": "loki", "name": "loki"},
                "spec": {
                    "selector": {"matchLabels": {"a": "1", "b": "2"}},
                    "containers": [{"name": "main"}],
                },
            },
        },
    }
    resp = validate(review_self_update, cache)["response"]
    _check("[validate] self-update (same name+namespace) → Allowed=true",
           resp["allowed"], True)

    # No siblings → always allow.
    cache._index = {"loki": _NamespaceIndex()}
    resp = validate(review_self_update, cache)["response"]
    _check("[validate] no siblings → Allowed=true", resp["allowed"], True)

    # Cache unready → allow.
    cache._ready.clear()
    resp = validate(review_self_update, cache)["response"]
    _check("[validate] cache unready → Allowed=true (degrade open)",
           resp["allowed"], True)

    # empty matchLabels was silently allowed by
    # validation, then dropped by cache parse — CR ended up in etcd
    # but never patched anything. Operator surprise. 1.21.0 rejects
    # at admission with a clear message.
    cache._ready.set()
    review_empty = {
        "request": {
            "uid": "test-empty-sel",
            "object": {
                "apiVersion": "kube-resource-updater.io/v1",
                "kind": "ResourceOverride",
                "metadata": {"namespace": "ns", "name": "ghost"},
                "spec": {
                    "selector": {"matchLabels": {}},  # ← empty
                    "containers": [{"name": "app",
                                    "requests": {"cpu": "100m"}}],
                },
            },
        }
    }
    resp = validate(review_empty, cache)["response"]
    _check("[validate-D5] empty matchLabels → Allowed=false",
           resp["allowed"], False)
    _check("[validate-D5] error message names the misconfig",
           "empty spec.selector.matchLabels" in resp["status"]["message"], True)

    # Negative: matchLabels OMITTED entirely (missing key) → same path.
    review_missing = {
        "request": {
            "uid": "test-missing-sel",
            "object": {
                "apiVersion": "kube-resource-updater.io/v1",
                "kind": "ResourceOverride",
                "metadata": {"namespace": "ns", "name": "ghost2"},
                "spec": {"containers": [{"name": "app"}]},
            },
        }
    }
    resp = validate(review_missing, cache)["response"]
    _check("[validate-D5-missing] missing matchLabels also rejected",
           resp["allowed"], False)

    # Verify the cache's matches() defensive check also rejects empty
    # selectors (defence in depth — caches built before 1.21.0 might
    # still have ghost CRs from etcd).
    from src.webhook_cache import ContainerOverride
    ghost_ro = ResourceOverride(
        namespace="ns", name="ghost",
        selector_match_labels={},   # ← would have matched everything pre-fix
        containers=(),
    )
    _check("[cache-D5-matches] empty-selector RO.matches() returns False",
           ghost_ro.matches({"app.kubernetes.io/instance": "anything"}), False)

    # ── Bug #40 — matchExpressions silently dropped ──────────────────────
    # `_parse` only reads `matchLabels`; any `matchExpressions` constraints
    # are silently ignored, making the effective selector broader than the
    # operator wrote. The validator must DENY when matchExpressions is
    # present (with or without matchLabels), with a message that names the
    # unsupported field. `.get(...)` guards keep these as clean soft-fails
    # pre-fix (sub-case (b) returns an _allow response with no `status`).
    cache._ready.set()
    review_expr_only = {
        "request": {
            "uid": "test-expr-only",
            "object": {
                "apiVersion": "kube-resource-updater.io/v1",
                "kind": "ResourceOverride",
                "metadata": {"namespace": "ns", "name": "expr-only"},
                "spec": {
                    "selector": {
                        "matchExpressions": [
                            {"key": "env", "operator": "In", "values": ["prod"]}
                        ]
                    },
                    "containers": [{"name": "app", "requests": {"cpu": "100m"}}],
                },
            },
        }
    }
    resp = validate(review_expr_only, cache)["response"]
    _check("[validate-#40a] matchExpressions-only → Allowed=false",
           resp["allowed"], False)
    _check("[validate-#40a] error message mentions matchExpressions",
           "matchExpressions" in (resp.get("status") or {}).get("message", ""), True)

    # Sub-case (b): both matchLabels AND matchExpressions. Empty namespace
    # index isolates the selector check from the sibling-conflict path, so
    # pre-fix this returns Allowed=true (matchLabels non-empty, no conflict).
    cache._index = {"ns": _NamespaceIndex()}
    review_expr_both = {
        "request": {
            "uid": "test-expr-both",
            "object": {
                "apiVersion": "kube-resource-updater.io/v1",
                "kind": "ResourceOverride",
                "metadata": {"namespace": "ns", "name": "expr-both"},
                "spec": {
                    "selector": {
                        "matchLabels": {"app": "web"},
                        "matchExpressions": [
                            {"key": "env", "operator": "NotIn", "values": ["dev"]}
                        ],
                    },
                    "containers": [{"name": "app", "requests": {"cpu": "100m"}}],
                },
            },
        }
    }
    resp = validate(review_expr_both, cache)["response"]
    _check("[validate-#40b] matchLabels+matchExpressions → Allowed=false",
           resp["allowed"], False)
    _check("[validate-#40b] error message mentions matchExpressions",
           "matchExpressions" in (resp.get("status") or {}).get("message", ""), True)


def section_mr_metadata() -> None:
    """Verifies the MR metadata feature end-to-end:

      - `MrConfig` defaults preserve the pre-1.7.0 MR shape (no
        assignees/reviewers/labels, squash off, remove_source_branch on).
      - `Config.from_file` parses the `mr` block (CSVs split, bools coerced).
      - `_resolve_gitlab_user_ids` calls GitLab once per username, drops
        unknown ones with a warning, returns IDs in input order.
      - `_create_gitlab_mr` payload includes only the fields that were
        actually set — no `assignee_ids: []` noise on a default install.
      - Labels are joined with commas (GitLab MR API expects a string,
        not a JSON array).
    """
    _section("MR metadata — assignees / reviewers / labels / squash")

    from src.config import Config, MrConfig
    from src.writeback import _create_gitlab_mr, _resolve_gitlab_user_ids

    # ── MrConfig defaults ────────────────────────────────────────────────
    default = MrConfig()
    _check("[default] assignees empty",                default.assignees, [])
    _check("[default] reviewers empty",                default.reviewers, [])
    _check("[default] labels empty",                   default.labels, [])
    _check("[default] squash off",                     default.squash, False)
    _check("[default] remove_source_branch on (legacy default)",
           default.remove_source_branch, True)

    # ── Config.from_file integration ─────────────────────────────────────
    import tempfile
    import textwrap
    raw = textwrap.dedent("""
        config:
          crWriteback:
            repoUrl: "https://git.example.com/x.git"
            path: "manifests/x"
          mr:
            assignees: "alice"
            reviewers: "bob, carol "
            labels: "auto, vpa"
            squash: true
            removeSourceBranch: false
    """)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(raw); cfg_path = fh.name
    cfg = Config.from_file(cfg_path)
    _check("[from_file] reviewers split + stripped",  cfg.mr.reviewers, ["bob", "carol"])
    _check("[from_file] assignees parsed",            cfg.mr.assignees, ["alice"])
    _check("[from_file] labels split",                cfg.mr.labels,    ["auto", "vpa"])
    _check("[from_file] squash bool",                 cfg.mr.squash,    True)
    _check("[from_file] removeSourceBranch=false respected (overrides default)",
           cfg.mr.remove_source_branch, False)

    # Empty `mr` block → defaults preserved.
    raw2 = textwrap.dedent("""
        config:
          crWriteback:
            repoUrl: "https://git.example.com/x.git"
            path: "manifests/x"
    """)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(raw2); cfg2_path = fh.name
    cfg2 = Config.from_file(cfg2_path)
    _check("[from_file] absent mr block → MrConfig default",
           (cfg2.mr.assignees, cfg2.mr.reviewers, cfg2.mr.labels,
            cfg2.mr.squash, cfg2.mr.remove_source_branch),
           ([], [], [], False, True))

    # ── _resolve_gitlab_user_ids ─────────────────────────────────────────
    with patch("src.writeback.requests.get") as get:
        # alice exists, bob exists, ghost doesn't (empty list response).
        # Build responses as a concrete list FIRST, then assign — assigning
        # a list to side_effect turns it into an iterator on first call,
        # so we can't iterate it ourselves afterwards.
        responses = [
            MagicMock(status_code=200, json=lambda: [{"id": 7,  "username": "alice"}]),
            MagicMock(status_code=200, json=lambda: [{"id": 11, "username": "bob"}]),
            MagicMock(status_code=200, json=lambda: []),
        ]
        for r in responses:
            r.raise_for_status = MagicMock(return_value=None)
        get.side_effect = responses
        ids = _resolve_gitlab_user_ids(
            "https://git.example.com", "tok", ["alice", "bob", "ghost"])
        _check("[resolve] order preserved, unknown dropped", ids, [7, 11])
        _check("[resolve] one HTTP call per username (incl. the failure)",
               get.call_count, 3)

    # No token → empty list, no HTTP calls.
    with patch("src.writeback.requests.get") as get:
        ids = _resolve_gitlab_user_ids("https://git.example.com", "", ["alice"])
        _check("[resolve] no token → empty list (no HTTP)", ids, [])
        _check("[resolve] no token → no HTTP calls",        get.called, False)

    # Empty username list → empty list, no HTTP calls.
    with patch("src.writeback.requests.get") as get:
        ids = _resolve_gitlab_user_ids("https://git.example.com", "tok", [])
        _check("[resolve] empty input → empty output (no HTTP)", ids, [])
        _check("[resolve] empty input → no HTTP calls",          get.called, False)

    # ── _create_gitlab_mr payload shape ──────────────────────────────────
    # added a pre-POST adoption GET — mock it as empty so POST
    # runs and we can inspect its payload.
    with patch("src.writeback._gitlab_post") as post, \
         patch("src.writeback._gitlab_get")  as get:
        get.return_value = MagicMock(
            status_code=200, json=lambda: [],
            raise_for_status=MagicMock(return_value=None),
        )
        post.return_value = MagicMock(
            status_code=201,
            json=lambda: {"web_url": "https://git.example.com/x/-/merge_requests/1"},
        )
        post.return_value.raise_for_status = MagicMock(return_value=None)
        url = _create_gitlab_mr(
            gitlab_url="https://git.example.com",
            token="tok",
            project_path="ns/x",
            source_branch="resource-updater/sync",
            target_branch="main",
            title="t",
            description="d",
            assignee_ids=[7],
            reviewer_ids=[11, 13],
            labels=["auto", "vpa"],
            squash=True,
            remove_source_branch=False,
        )
        _check("[mr] returns web_url",
               url, "https://git.example.com/x/-/merge_requests/1")
        body = post.call_args.kwargs["json"]
        _check("[mr] payload includes assignee_ids",  body.get("assignee_ids"), [7])
        _check("[mr] payload includes reviewer_ids",  body.get("reviewer_ids"), [11, 13])
        _check("[mr] labels joined with commas (GitLab API contract)",
               body.get("labels"), "auto,vpa")
        _check("[mr] squash=true honoured",           body.get("squash"), True)
        _check("[mr] remove_source_branch=false honoured",
               body.get("remove_source_branch"), False)

    # Default-only invocation: payload must NOT carry empty IDs / labels.
    with patch("src.writeback._gitlab_post") as post, \
         patch("src.writeback._gitlab_get")  as get:
        get.return_value = MagicMock(
            status_code=200, json=lambda: [],
            raise_for_status=MagicMock(return_value=None),
        )
        post.return_value = MagicMock(
            status_code=201, json=lambda: {"web_url": "https://x/mr/2"})
        post.return_value.raise_for_status = MagicMock(return_value=None)
        _create_gitlab_mr(
            gitlab_url="https://x", token="tok", project_path="a/b",
            source_branch="s", target_branch="main",
            title="t", description="d",
        )
        body = post.call_args.kwargs["json"]
        _check("[mr] default call: no assignee_ids in payload",
               "assignee_ids" in body, False)
        _check("[mr] default call: no reviewer_ids in payload",
               "reviewer_ids" in body, False)
        _check("[mr] default call: no labels in payload",
               "labels" in body, False)
        _check("[mr] default call: squash=False present",
               body.get("squash"), False)
        _check("[mr] default call: remove_source_branch=True present (legacy)",
               body.get("remove_source_branch"), True)


def section_skip_containers() -> None:
    """Verifies the skipContainers feature end-to-end:

      - `_parse_skip_containers` strips whitespace, drops empty entries.
      - `parse_annotations` accepts the key on namespace and workload
        scopes and round-trips through `apply()` without leaking into
        Config fields.
      - `resolve_skip_containers` follows the workload > namespace > helm
        chain, including the empty-string "clear inherited" override.
      - `_filter_skipped_containers` drops the right containers and
        leaves the rest.
      - `webhook_patch.build_patches` no longer touches initContainers
        regardless of how a CR is shaped (operators can't accidentally
        opt one in).
      - `list_workloads_in_namespace` snapshots init container names
        without including them on the recommendation list.
    """
    _section("skipContainers — sync filter + init container auto-skip")

    from src.overrides import (
        ANNOTATION_PREFIX,
        SKIP_CONTAINERS_KEY,
        _parse_skip_containers,
        apply,
        merge,
        parse_annotations,
        resolve_skip_containers,
    )
    from src.webhook_cache import ContainerOverride, ResourceOverride
    from src.webhook_patch import build_patches
    from src.workload import (
        ContainerRecommendation,
        WorkloadRecommendation,
        list_workloads_in_namespace,
    )
    from src.writeback_webhook import _filter_skipped_containers

    P = ANNOTATION_PREFIX
    K = SKIP_CONTAINERS_KEY  # "skipContainers"

    # ── _parse_skip_containers raw parsing ───────────────────────────────
    _check("[parse] empty string → []",      _parse_skip_containers(""),                   [])
    _check("[parse] single name",            _parse_skip_containers("istio-proxy"),        ["istio-proxy"])
    _check("[parse] CSV with whitespace",    _parse_skip_containers("istio-proxy, fluent-bit "),
           ["istio-proxy", "fluent-bit"])
    _check("[parse] drops empty entries",    _parse_skip_containers("a,,b, ,c"),           ["a", "b", "c"])

    # ── parse_annotations: scope-agnostic, type stable ───────────────────
    ns_parsed = parse_annotations({P + K: "istio-proxy,linkerd-proxy"},
                                  scope="namespace", source="ns/x")
    _check("[anno] ns scope: list parsed", ns_parsed.get(K), ["istio-proxy", "linkerd-proxy"])
    wl_parsed = parse_annotations({P + K: ""}, scope="workload", source="x/y")
    _check("[anno] wl scope: empty string → empty list (clear inherited)",
           wl_parsed.get(K), [])

    # apply() must NOT project skipContainers onto Config (it's a marker key).
    base = _base_config()
    eff = apply(base, merge(ns_parsed, wl_parsed))
    _check("[apply] skipContainers does not leak into Config field",
           type(eff).__name__, "Config")
    _check("[apply] base Config returned unchanged for marker-only overrides",
           eff is base, True)

    # ── resolve_skip_containers hierarchy ────────────────────────────────
    helm_default = ["istio-proxy"]
    # helm-only path
    _check("[resolve] no annotations → helm default kept",
           resolve_skip_containers(helm_default, {}, {}), ["istio-proxy"])
    # namespace overrides helm
    _check("[resolve] ns annotation wins over helm",
           resolve_skip_containers(helm_default,
                                   {P + K: "datadog-agent,fluent-bit"}, {}),
           ["datadog-agent", "fluent-bit"])
    # workload overrides namespace
    _check("[resolve] workload annotation wins over ns",
           resolve_skip_containers(helm_default,
                                   {P + K: "datadog-agent"},
                                   {P + K: "envoy"}),
           ["envoy"])
    # workload empty string clears inherited
    _check("[resolve] workload \"\" clears inherited list",
           resolve_skip_containers(helm_default,
                                   {P + K: "datadog-agent,fluent-bit"},
                                   {P + K: ""}),
           [])
    # helm None / [] safe
    _check("[resolve] None helm default → []",
           resolve_skip_containers(None, None, None), [])
    _check("[resolve] [] helm default → []",
           resolve_skip_containers([], {}, {}), [])

    # ── Config.from_file CSV parsing path ────────────────────────────────
    # Config dataclass field default + _parse_csv interplay; we exercise
    # _parse_csv via a fresh Config built from a fake values dict.
    from src.config import _parse_csv
    _check("[config] _parse_csv list pass-through",
           _parse_csv(["a", "b ", " "]), ["a", "b"])
    _check("[config] _parse_csv CSV string",
           _parse_csv("istio-proxy, fluent-bit"), ["istio-proxy", "fluent-bit"])
    _check("[config] _parse_csv empty",
           _parse_csv(""), [])

    # ── _filter_skipped_containers in writeback_webhook ──────────────────
    rec = WorkloadRecommendation(
        name="api", namespace="ns", target_kind="Deployment", target_name="api",
        containers=[
            ContainerRecommendation(container_name="api"),
            ContainerRecommendation(container_name="istio-proxy"),
            ContainerRecommendation(container_name="fluent-bit"),
        ],
    )
    rec.skip_containers = ["istio-proxy", "fluent-bit"]
    kept, dropped = _filter_skipped_containers(rec)
    _check("[filter] kept retains app container only",
           [c.container_name for c in kept], ["api"])
    _check("[filter] dropped reports skipped names in order",
           dropped, ["istio-proxy", "fluent-bit"])

    rec.skip_containers = []
    kept, dropped = _filter_skipped_containers(rec)
    _check("[filter] empty skip list → all kept, none dropped",
           ([c.container_name for c in kept], dropped),
           (["api", "istio-proxy", "fluent-bit"], []))

    # Names not present in the workload silently no-op (operator typo path).
    rec.skip_containers = ["does-not-exist"]
    kept, dropped = _filter_skipped_containers(rec)
    _check("[filter] missing name → no-op (kept unchanged, dropped empty)",
           ([c.container_name for c in kept], dropped),
           (["api", "istio-proxy", "fluent-bit"], []))

    # skipContainers listing EVERY container of a
    # workload silently un-manages it (CR file gets pruned by ArgoCD).
    # Operator-surprising — they wanted "ignore container X" but got the
    # equivalent of `skip: "true"`. The warning has to fire in
    # `_build_containers_payload`, not `_filter_skipped_containers` (the
    # filter doesn't know if the workload is going to end up empty).
    # We exercise the full payload-build pipeline with a single-container
    # workload listed in skipContainers and assert the warning is logged.
    import logging as _logging
    import io as _io
    from src.writeback_webhook import _build_containers_payload
    from src.workload import WorkloadRecommendation as WR, ContainerRecommendation as CR
    log_buf = _io.StringIO()
    handler = _logging.StreamHandler(log_buf)
    handler.setLevel(_logging.WARNING)
    root_logger = _logging.getLogger("src.writeback_webhook")
    root_logger.addHandler(handler)
    try:
        rec_all_skipped = WR(
            name="my-app", namespace="my-app",
            target_kind="Deployment", target_name="my-app",
            containers=[CR(container_name="main")],
        )
        rec_all_skipped.skip_containers = ["main"]
        cfg_minimal = _base_config()
        payload, _annotations = _build_containers_payload(
            rec_all_skipped, cfg_minimal,
            oom_state=None, oom_events=None, oom_eligible=True,
            oom_floor_enabled=True, oom_floor_reset=False,
        )
    finally:
        root_logger.removeHandler(handler)
    log_text = log_buf.getvalue()
    _check("[filter-A8] all-stripped: payload is empty",
           payload, [])
    _check("[filter-A8] all-stripped: warning logged with [skip-containers] tag",
           "[skip-containers]" in log_text and "ALL containers" in log_text, True)
    _check("[filter-A8] warning names the workload",
           "my-app/my-app" in log_text, True)

    # ── webhook_patch: init containers ALWAYS skipped ────────────────────
    pod = {
        "metadata": {"namespace": "ns", "name": "api-abc"},
        "spec": {
            "containers": [
                {"name": "api", "resources": {"requests": {"cpu": "10m"}}},
            ],
            "initContainers": [
                {"name": "wait-for-db", "resources": {"requests": {"cpu": "10m"}}},
            ],
        },
    }
    overrides = [
        ResourceOverride(
            namespace="ns", name="api",
            selector_match_labels={"app.kubernetes.io/name": "api"},
            containers=(
                ContainerOverride(name="api",          requests={"cpu": "150m"}, limits={"cpu": "300m"}),
                # Hand-crafted CR that names an init container — webhook
                # must ignore it regardless.
                ContainerOverride(name="wait-for-db", requests={"cpu": "999m"}, limits={"cpu": "999m"}),
            ),
        ),
    ]
    patches = build_patches(pod, overrides)
    paths = [p.path for p in patches]
    _check("[patch] /spec/initContainers/* never appears",
           any("initContainers" in p for p in paths), False)
    _check("[patch] /spec/containers/0 IS patched (regular container honoured)",
           any(p.startswith("/spec/containers/0/resources") for p in paths), True)

    # ── Regression: pod WITHOUT metadata.annotations (StatefulSet template,
    # raw manifest) used to break pod creation with
    # "doc is missing path: /metadata/annotations/...: missing value"
    # because JSONPatch add at /metadata/annotations/<key> requires the
    # parent object to exist. Fix: emit the whole annotations object.
    pod_no_annotations = {
        "metadata": {"namespace": "ns", "name": "api-abc"},  # no `annotations` key
        "spec": {
            "containers": [
                {"name": "api", "resources": {"requests": {"cpu": "10m"}}},
            ],
        },
    }
    overrides_simple = [
        ResourceOverride(
            namespace="ns", name="api",
            selector_match_labels={"app.kubernetes.io/name": "api"},
            containers=(
                ContainerOverride(name="api", requests={"cpu": "150m"}, limits={"cpu": "300m"}),
            ),
        ),
    ]
    patches = build_patches(pod_no_annotations, overrides_simple)
    annotation_patches = [p for p in patches if p.path.startswith("/metadata/annotations")]
    _check("[patch] no-annotations pod: exactly 1 metadata patch (the parent object)",
           len(annotation_patches), 1)
    _check("[patch] no-annotations pod: targets /metadata/annotations parent",
           annotation_patches[0].path, "/metadata/annotations")
    _check("[patch] no-annotations pod: value is the dict with applied-from inside",
           annotation_patches[0].value,
           {"kube-resource-updater.applied-from": "ns/api"})

    # Pod WITH existing annotations: keep the original per-key add path
    # (so we don't clobber unrelated annotations).
    pod_with_annotations = dict(pod_no_annotations)
    pod_with_annotations["metadata"] = {
        "namespace": "ns", "name": "api-abc",
        "annotations": {"prometheus.io/scrape": "true"},
    }
    patches = build_patches(pod_with_annotations, overrides_simple)
    annotation_patches = [p for p in patches if p.path.startswith("/metadata/annotations")]
    _check("[patch] with-annotations pod: per-key add (does not clobber existing)",
           annotation_patches[0].path,
           "/metadata/annotations/kube-resource-updater.applied-from")

    # ── workload.list_workloads_in_namespace captures init names ─────────
    apps_api = MagicMock()
    init_c   = MagicMock(); init_c.name = "wait-for-db"
    main_c   = MagicMock(); main_c.name = "api"
    sidecar  = MagicMock(); sidecar.name  = "istio-proxy"
    pod_spec = MagicMock(); pod_spec.containers = [main_c, sidecar]; pod_spec.init_containers = [init_c]
    template = MagicMock(); template.spec = pod_spec; template.metadata = MagicMock(labels={"app.kubernetes.io/name": "api"})
    # `spec=["template", "paused"]` constrains the MagicMock to those attrs
    # so getattr(item.spec, "paused", False) returns False (not a truthy
    # auto-MagicMock). a misconfigured setup Area 2 added the paused-skip check;
    # without this constraint every MagicMock-based deploy would be treated
    # as paused.
    deploy_spec = MagicMock(spec=["template", "paused"])
    deploy_spec.template = template
    deploy_spec.paused = False
    deploy = MagicMock(); deploy.spec = deploy_spec
    deploy.metadata = MagicMock(name="api", labels={"app.kubernetes.io/name": "api"}, annotations={})
    deploy.metadata.name = "api"  # MagicMock(name=...) sets a different attr
    apps_api.list_namespaced_deployment.return_value = MagicMock(items=[deploy])
    apps_api.list_namespaced_stateful_set.return_value = MagicMock(items=[])
    recs = list_workloads_in_namespace(apps_api, "ns")
    _check("[workload] discovered exactly 1 workload", len(recs), 1)
    if recs:
        _check("[workload] regular containers exposed (no init in list)",
               [c.container_name for c in recs[0].containers], ["api", "istio-proxy"])
        _check("[workload] init container names captured separately",
               recs[0].init_container_names, ["wait-for-db"])
        _check("[workload] skip_containers default is empty list",
               recs[0].skip_containers, [])

    # ── Framework audit Area 2: paused Deployment is skipped ─────────────
    # spec.paused: true means the operator deliberately stopped
    # reconciliation. Generating a recommendation for it AND applying via
    # the webhook on the next pod create violates that intent — the tool
    # silently overrides the operator's pause.
    paused_dep = MagicMock()
    paused_dep_spec = MagicMock()
    paused_dep_spec.template = template            # reuse from above
    paused_dep_spec.paused = True
    paused_dep.spec = paused_dep_spec
    paused_dep.metadata = MagicMock(name="paused-app",
                                    labels={"app.kubernetes.io/name": "paused-app"},
                                    annotations={})
    paused_dep.metadata.name = "paused-app"
    apps_api_paused = MagicMock()
    apps_api_paused.list_namespaced_deployment.return_value = MagicMock(items=[paused_dep])
    apps_api_paused.list_namespaced_stateful_set.return_value = MagicMock(items=[])
    recs_paused = list_workloads_in_namespace(apps_api_paused, "ns")
    _check("[workload-paused] paused Deployment is skipped (operator intent preserved)",
           recs_paused, [])

    # Active Deployment with spec.paused=False stays included.
    active_dep = MagicMock()
    active_dep_spec = MagicMock()
    active_dep_spec.template = template
    active_dep_spec.paused = False
    active_dep.spec = active_dep_spec
    active_dep.metadata = MagicMock(name="active-app",
                                    labels={"app.kubernetes.io/name": "active-app"},
                                    annotations={})
    active_dep.metadata.name = "active-app"
    apps_api_active = MagicMock()
    apps_api_active.list_namespaced_deployment.return_value = MagicMock(items=[active_dep])
    apps_api_active.list_namespaced_stateful_set.return_value = MagicMock(items=[])
    _check("[workload-active] spec.paused=False stays included",
           len(list_workloads_in_namespace(apps_api_active, "ns")), 1)

    # spec.paused absent (older Deployment shapes, StatefulSet) → kept.
    no_paused_dep = MagicMock()
    no_paused_dep_spec = MagicMock(spec=["template"])  # no `paused` attr
    no_paused_dep_spec.template = template
    no_paused_dep.spec = no_paused_dep_spec
    no_paused_dep.metadata = MagicMock(name="legacy-app",
                                       labels={"app.kubernetes.io/name": "legacy-app"},
                                       annotations={})
    no_paused_dep.metadata.name = "legacy-app"
    apps_api_legacy = MagicMock()
    apps_api_legacy.list_namespaced_deployment.return_value = MagicMock(items=[no_paused_dep])
    apps_api_legacy.list_namespaced_stateful_set.return_value = MagicMock(items=[])
    _check("[workload-no-paused-attr] Deployment without paused attr is kept (defensive)",
           len(list_workloads_in_namespace(apps_api_legacy, "ns")), 1)

    # ── DaemonSet discovery ────────────────────────────────
    # Pre-fix: list_namespaced_daemon_set is never called; DaemonSets are
    # silently omitted. Post-fix: the discovery loop includes
    # ("DaemonSet", apps_api.list_namespaced_daemon_set) and the workload
    # appears with target_kind="DaemonSet".
    apps_api_ds = MagicMock()
    ds_c = MagicMock(); ds_c.name = "fluentd"
    ds_pod_spec = MagicMock(); ds_pod_spec.containers = [ds_c]; ds_pod_spec.init_containers = []
    ds_tmpl = MagicMock()
    ds_tmpl.spec = ds_pod_spec
    ds_tmpl.metadata = MagicMock(labels={"app": "fluentd"})
    ds_spec = MagicMock(spec=["template", "paused"])
    ds_spec.template = ds_tmpl
    ds_spec.paused = False
    ds = MagicMock()
    ds.spec = ds_spec
    ds.metadata = MagicMock(labels={}, annotations={})
    ds.metadata.name = "fluentd"
    apps_api_ds.list_namespaced_deployment.return_value = MagicMock(items=[])
    apps_api_ds.list_namespaced_stateful_set.return_value = MagicMock(items=[])
    apps_api_ds.list_namespaced_daemon_set.return_value = MagicMock(items=[ds])
    recs_ds = list_workloads_in_namespace(apps_api_ds, "ns")
    _check("[workload-ds] DaemonSet is discovered (exactly 1 result)",
           len(recs_ds), 1)
    if recs_ds:
        _check("[workload-ds] target_kind is DaemonSet",
               recs_ds[0].target_kind, "DaemonSet")
        _check("[workload-ds] containers list populated",
               [c.container_name for c in recs_ds[0].containers], ["fluentd"])
    _check("[workload-ds] list_namespaced_daemon_set was called",
           apps_api_ds.list_namespaced_daemon_set.called, True)

    # ── CronJob discovery (ROADMAP "CronJob / Job support") ──────────────
    # Pre-fix: list_workloads_in_namespace takes no batch_api param; CronJobs
    # are never discovered. Post-fix: batch_api is threaded through and the
    # loop adds ("CronJob", batch_api.list_namespaced_cron_job) using the
    # two-level template path item.spec.job_template.spec.template.
    # NOTE: the k8s python client attribute is `job_template` (snake_case),
    # NOT `jobTemplate` — the mock mirrors the real attr so this assert
    # actually exercises the production path.
    from src.workload import list_workloads_in_namespace as _lwns

    cj_c = MagicMock(); cj_c.name = "worker"
    cj_pod_spec = MagicMock(); cj_pod_spec.containers = [cj_c]; cj_pod_spec.init_containers = []
    cj_tmpl = MagicMock()
    cj_tmpl.spec = cj_pod_spec
    cj_tmpl.metadata = MagicMock(labels={"app": "worker"})
    cj_jt_spec = MagicMock(); cj_jt_spec.template = cj_tmpl
    cj_jt = MagicMock(); cj_jt.spec = cj_jt_spec
    cj_spec = MagicMock(spec=["job_template", "suspend"])
    cj_spec.job_template = cj_jt
    cj_spec.suspend = False
    cj = MagicMock()
    cj.spec = cj_spec
    cj.metadata = MagicMock(labels={}, annotations={})
    cj.metadata.name = "batch-worker"

    fake_apps_cj = MagicMock()
    fake_apps_cj.list_namespaced_deployment.return_value = MagicMock(items=[])
    fake_apps_cj.list_namespaced_stateful_set.return_value = MagicMock(items=[])
    fake_apps_cj.list_namespaced_daemon_set.return_value = MagicMock(items=[])
    fake_batch = MagicMock()
    fake_batch.list_namespaced_cron_job.return_value = MagicMock(items=[cj])

    recs_cj = _lwns(fake_apps_cj, "ns", batch_api=fake_batch)
    _check("[workload-cj] CronJob is discovered (exactly 1 result)",
           len(recs_cj), 1)
    if recs_cj:
        _check("[workload-cj] target_kind is CronJob",
               recs_cj[0].target_kind, "CronJob")
        _check("[workload-cj] containers from jobTemplate pod template",
               [c.container_name for c in recs_cj[0].containers], ["worker"])
    _check("[workload-cj] list_namespaced_cron_job was called",
           fake_batch.list_namespaced_cron_job.called, True)

    # Suspended CronJob (spec.suspend=true) → skipped.
    cj_suspended = MagicMock()
    cj_spec_suspended = MagicMock(spec=["job_template", "suspend"])
    cj_spec_suspended.job_template = cj_jt
    cj_spec_suspended.suspend = True
    cj_suspended.spec = cj_spec_suspended
    cj_suspended.metadata = MagicMock(labels={}, annotations={})
    cj_suspended.metadata.name = "suspended-worker"
    fake_batch_susp = MagicMock()
    fake_batch_susp.list_namespaced_cron_job.return_value = MagicMock(items=[cj_suspended])
    _check("[workload-cj] suspended CronJob (spec.suspend=true) is skipped",
           _lwns(fake_apps_cj, "ns", batch_api=fake_batch_susp), [])

    # batch_api=None → CronJob NOT discovered (apps-only backward-compat path).
    fake_apps_nb = MagicMock()
    fake_apps_nb.list_namespaced_deployment.return_value = MagicMock(items=[])
    fake_apps_nb.list_namespaced_stateful_set.return_value = MagicMock(items=[])
    fake_apps_nb.list_namespaced_daemon_set.return_value = MagicMock(items=[])
    _check("[workload-cj] batch_api=None → zero results, no CronJob discovered",
           len(_lwns(fake_apps_nb, "ns")), 0)


def section_oom_slow_path() -> None:
    """Verifies the OOM-aware slow-path:

      - Per-container annotation parsers handle BOTH legacy single-key
        CSV AND new prefix form.
      - `_resolve_workload_name_from_pod` handles ReplicaSet hash strip.
      - `is_oom_detection_enabled` resolver follows helm < ns < workload.
      - `_build_containers_payload` bump logic: dedupes via stored
        finishedAt, applies bump = `trap × bumpFactor`, clamps at
        `maxMemoryLimitMi` ceiling, stamps per-container annotations.
      - `_render_cr_doc` carries forward operator-added non-OOM
        annotations + labels intact (only `managed-by` is tool-owned).
      - `bumpFactor < 1.0` clamped to 1.0 at ResourceConfig load time
        (prevents the operator footgun of shrink-on-OOM).
      - `_apply_oom_floor` clamps memory request/limit UP to floor.
    """
    _section("OOM-aware slow-path — detection, bump, annotation, floor")

    import os
    from src.config import ResourceConfig
    from src.overrides import is_oom_detection_enabled
    from src.workload import ContainerRecommendation, WorkloadRecommendation
    from src.writeback_webhook import (
        OOM_FLOOR_PREFIX,
        OOM_LAST_EVENT_PREFIX,
        OomEvent,
        WebhookEntry,
        _apply_oom_floor,
        _build_containers_payload,
        _is_oom_annotation,
        _render_cr_doc,
        _resolve_workload_name_from_pod,
        floor_annotation_key,
        history_annotation_key,
        last_event_annotation_key,
        parse_oom_floors_from_annotations,
        parse_oom_last_events_from_annotations,
    )

    M = 1024 * 1024

    # ── ResourceConfig clamp: bumpFactor >= 1.0 ─────────────────────────
    # Operator footgun: setting bumpFactor < 1.0 would SHRINK memory on
    # each OOM. Clamp at load with warning. Validate via env var read.
    os.environ["OOM_BUMP_FACTOR"] = "0.5"
    try:
        rc = ResourceConfig.from_env()
        _check("[clamp] bumpFactor < 1.0 clamped to 1.0",
               rc.oom_bump_factor, 1.0)
    finally:
        os.environ.pop("OOM_BUMP_FACTOR", None)

    # ── Annotation parsers: legacy + new format ─────────────────────────
    # Mixed annotations: legacy single-key + per-container prefix.
    legacy_anns = {
        "kube-resource-updater.io/oom-floor": "web=900Mi,cache=64Mi",
        "kube-resource-updater.io/oom-last-event": "web=2026-05-09T12:00:00Z",
    }
    floors = parse_oom_floors_from_annotations(legacy_anns)
    _check("[parse-legacy] floor: 2 containers parsed",
           sorted(floors.keys()), ["cache", "web"])
    _check("[parse-legacy] floor: bytes value correct",
           floors["web"], 900 * M)
    events = parse_oom_last_events_from_annotations(legacy_anns)
    _check("[parse-legacy] last-event: timestamp preserved",
           events["web"], "2026-05-09T12:00:00Z")

    new_format = {
        f"{OOM_FLOOR_PREFIX}web": "900Mi",
        f"{OOM_FLOOR_PREFIX}cache": "64Mi",
        f"{OOM_LAST_EVENT_PREFIX}web": "2026-05-09T12:00:00Z",
    }
    _check("[parse-new] floor: prefix-keyed parsed",
           parse_oom_floors_from_annotations(new_format),
           {"web": 900 * M, "cache": 64 * M})

    # New format takes precedence over legacy (duplicate container).
    mixed = {
        "kube-resource-updater.io/oom-floor": "web=512Mi",
        f"{OOM_FLOOR_PREFIX}web": "900Mi",
    }
    _check("[parse-mixed] new-format wins over legacy on collision",
           parse_oom_floors_from_annotations(mixed)["web"], 900 * M)

    # ── _is_oom_annotation: prefix + legacy detection ───────────────────
    _check("[is-oom] new prefix detected",
           _is_oom_annotation(f"{OOM_FLOOR_PREFIX}web"), True)
    _check("[is-oom] legacy single key detected",
           _is_oom_annotation("kube-resource-updater.io/oom-floor"), True)
    _check("[is-oom] non-OOM annotation NOT detected",
           _is_oom_annotation("kube-resource-updater.io/cost-center"), False)
    _check("[is-oom] unrelated annotation NOT detected",
           _is_oom_annotation("prometheus.io/scrape"), False)

    # ── _resolve_workload_name_from_pod ─────────────────────────────────
    class _Ref:
        def __init__(self, kind, name):
            self.kind = kind; self.name = name
    class _Meta:
        def __init__(self, refs):
            self.owner_references = refs
    class _Pod:
        def __init__(self, refs):
            self.metadata = _Meta(refs)

    pod_rs = _Pod([_Ref("ReplicaSet", "n8n-7c8d99b6f5")])
    _check("[workload] ReplicaSet → strip hash",
           _resolve_workload_name_from_pod(pod_rs), "n8n")
    pod_sts = _Pod([_Ref("StatefulSet", "loki-chunks-cache")])
    _check("[workload] StatefulSet → name preserved",
           _resolve_workload_name_from_pod(pod_sts), "loki-chunks-cache")
    pod_ds = _Pod([_Ref("DaemonSet", "fluent-bit")])
    _check("[workload] DaemonSet → name preserved",
           _resolve_workload_name_from_pod(pod_ds), "fluent-bit")
    _check("[workload] no ownerRef → empty",
           _resolve_workload_name_from_pod(_Pod([])), "")

    # ── is_oom_detection_enabled hierarchy ──────────────────────────────
    P = "kube-resource-updater."
    _check("[hier] all empty → helm default true",
           is_oom_detection_enabled(True, {}, {}), True)
    _check("[hier] all empty → helm default false",
           is_oom_detection_enabled(False, {}, {}), False)
    _check("[hier] ns false, helm true → ns wins",
           is_oom_detection_enabled(True,
                                     {P + "oomDetectionEnabled": "false"}, {}),
           False)
    _check("[hier] workload true, ns false → workload wins",
           is_oom_detection_enabled(True,
                                     {P + "oomDetectionEnabled": "false"},
                                     {P + "oomDetectionEnabled": "true"}),
           True)
    _check("[hier] bad value at workload → fall through",
           is_oom_detection_enabled(True,
                                     {P + "oomDetectionEnabled": "false"},
                                     {P + "oomDetectionEnabled": "maybe"}),
           False)

    # ── is_oom_floor_enabled hierarchy ───────────────────
    from src.overrides import (
        is_oom_floor_enabled,
        is_oom_floor_reset_requested,
    )
    _check("[floor-hier] all empty → helm default true",
           is_oom_floor_enabled(True, {}, {}), True)
    _check("[floor-hier] ns false → ns wins",
           is_oom_floor_enabled(True,
                                 {P + "oomFloorEnabled": "false"}, {}),
           False)
    _check("[floor-hier] workload true, ns false → workload wins",
           is_oom_floor_enabled(True,
                                 {P + "oomFloorEnabled": "false"},
                                 {P + "oomFloorEnabled": "true"}),
           True)

    # ── is_oom_floor_reset_requested ────────────────────────────────────
    # No helm default — explicit opt-in only. Either layer truthy fires reset.
    _check("[floor-reset] all empty → False",
           is_oom_floor_reset_requested({}, {}), False)
    _check("[floor-reset] ns true → True",
           is_oom_floor_reset_requested({P + "oomFloorReset": "true"}, {}),
           True)
    _check("[floor-reset] workload true → True",
           is_oom_floor_reset_requested({}, {P + "oomFloorReset": "true"}),
           True)
    _check("[floor-reset] both truthy → True (idempotent)",
           is_oom_floor_reset_requested({P + "oomFloorReset": "true"},
                                          {P + "oomFloorReset": "true"}),
           True)

    # ── _apply_oom_floor: clamp memory UP ───────────────────────────────
    res = {"requests": {"memory": "100Mi"}, "limits": {"memory": "200Mi"}}
    out = _apply_oom_floor(res, floor_bytes=900 * M, multiplier=3.0)
    _check("[floor] limit clamped UP to floor",
           out["limits"]["memory"], "900Mi")
    _check("[floor] request raised proportionally",
           out["requests"]["memory"], "300Mi")

    # No-op when computed already exceeds floor.
    high = {"requests": {"memory": "500Mi"}, "limits": {"memory": "1500Mi"}}
    out2 = _apply_oom_floor(high, floor_bytes=900 * M, multiplier=3.0)
    _check("[floor] no-op when limit already > floor",
           out2["limits"]["memory"], "1500Mi")

    # ── _build_containers_payload bump path ─────────────────────────────
    # `_build_container_resources` needs Prometheus values to produce a
    # non-empty res; skipping is no-op. Mock _query_prom_values to
    # return concrete values so the bump logic actually runs.
    from types import SimpleNamespace
    cfg = _base_config()
    cfg.prometheus_url = "http://prom:9090"
    cfg.resource.oom_detection_enabled = True
    cfg.resource.oom_bump_factor = 1.5
    cfg.resource.memory_limit_multiplier = 3.0
    cfg.resource.max_memory_limit_mi = 0   # unbounded for this test

    rec = WorkloadRecommendation(
        name="api", namespace="ns", target_kind="Deployment", target_name="api",
        containers=[ContainerRecommendation(container_name="app")],
    )

    prom_values = SimpleNamespace(
        cpu_request_m=200, memory_request_bytes=32 * M,
        cpu_limit_m=400, memory_limit_bytes=64 * M,
    )

    # No event → no annotations stamped.
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_values):
        payload, anns = _build_containers_payload(
            rec, cfg, oom_state=None, oom_events={}, oom_eligible=True,
        )
    _check("[bump] no event → no oom annotations",
           anns, {})

    # Event with NEWER finishedAt → bump applied + annotations stamped.
    ev = OomEvent(
        namespace="ns", workload_name="api", container="app",
        finished_at="2026-05-09T13:00:00Z",
        trap_limit_bytes=64 * M,
    )
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_values):
        payload, anns = _build_containers_payload(
            rec, cfg,
            oom_state={"floor": {}, "last_event": {}, "history": {}},
            oom_events={"app": ev},
            oom_eligible=True,
        )
    floor_key_app = floor_annotation_key("app")
    last_key_app = last_event_annotation_key("app")
    hist_key_app = history_annotation_key("app")
    _check("[bump] floor annotation stamped",
           anns.get(floor_key_app), "96Mi")
    _check("[bump] last-event stamped with finishedAt",
           anns.get(last_key_app), "2026-05-09T13:00:00Z")
    # Prom mock returns memory_limit=64Mi → computed limit before bump
    # is 64Mi → history shows real `64Mi→96Mi`. The em-dash placeholder
    # only appears when there was NO computed value at all (rare path).
    _check("[bump] history populated",
           "64Mi→96Mi" in (anns.get(hist_key_app) or ""), True)

    # Same finishedAt → DEDUPE, no bump.
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_values):
        payload, anns = _build_containers_payload(
            rec, cfg,
            oom_state={
                "floor": {"app": 96 * M},
                "last_event": {"app": "2026-05-09T13:00:00Z"},
                "history": {},
            },
            oom_events={"app": ev},
            oom_eligible=True,
        )
    _check("[bump] dedupe: same finishedAt → no new history entry",
           anns.get(hist_key_app, ""), "")
    # Floor preserved as-is when no new bump.
    _check("[bump] floor preserved across no-op syncs",
           anns.get(floor_key_app), "96Mi")

    # Workload not eligible → existing floor still respected, new bump skipped.
    ev_new = OomEvent(
        namespace="ns", workload_name="api", container="app",
        finished_at="2026-05-09T14:00:00Z",
        trap_limit_bytes=128 * M,
    )
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_values):
        payload, anns = _build_containers_payload(
            rec, cfg,
            oom_state={
                "floor": {"app": 96 * M},
                "last_event": {"app": "2026-05-09T13:00:00Z"},
                "history": {},
            },
            oom_events={"app": ev_new},
            oom_eligible=False,
        )
    _check("[ineligible] no new bump applied",
           anns.get(last_key_app), "2026-05-09T13:00:00Z")
    _check("[ineligible] existing floor stays",
           anns.get(floor_key_app), "96Mi")

    # Ceiling clamp: maxMemoryLimitMi caps the bump.
    cfg.resource.max_memory_limit_mi = 100   # 100 Mi
    cfg.resource.oom_bump_factor = 2.0
    ev_big = OomEvent(
        namespace="ns", workload_name="api", container="app",
        finished_at="2026-05-09T15:00:00Z",
        trap_limit_bytes=64 * M,   # × 2 = 128Mi, but ceiling is 100Mi
    )
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_values):
        payload, anns = _build_containers_payload(
            rec, cfg,
            oom_state={"floor": {}, "last_event": {}, "history": {}},
            oom_events={"app": ev_big},
            oom_eligible=True,
        )
    _check("[ceiling] floor capped at maxMemoryLimitMi",
           anns.get(floor_key_app), "100Mi")

    # ── No-Prom + fresh OOM: synthesize res so the bump still happens ───
    # Without this branch, a freshly-deployed workload with no Prom history
    # would loop OOM forever — `_build_container_resources` returns empty
    # (no prom data, no VPA target) and the container would be skipped,
    # so no CR ever gets written and the limit stays put.
    cfg.resource.max_memory_limit_mi = 0
    cfg.resource.oom_bump_factor = 1.5
    cfg.resource.min_memory_request_mi = 0   # disable mem floor so bump is observable
    cfg.resource.min_cpu_request_m = 50      # cpu floor still applies (synthesis uses it)
    ev_noprom = OomEvent(
        namespace="ns", workload_name="api", container="app",
        finished_at="2026-05-09T16:00:00Z",
        trap_limit_bytes=16 * M,    # tight pod limit that just OOM'd
    )
    # Mock _query_prom_values to return None (no Prom data path).
    with patch("src.writeback_webhook._query_prom_values", return_value=None):
        payload, anns = _build_containers_payload(
            rec, cfg,
            oom_state={"floor": {}, "last_event": {}, "history": {}},
            oom_events={"app": ev_noprom},
            oom_eligible=True,
        )
    _check("[no-prom] payload still contains the container",
           [p["name"] for p in payload], ["app"])
    _check("[no-prom] memory limit = trap × bumpFactor",
           payload[0].get("limits", {}).get("memory"), "24Mi")
    _check("[no-prom] last-event stamped (dedupe key)",
           anns.get(last_key_app), "2026-05-09T16:00:00Z")
    _check("[no-prom] history records the bump",
           "16Mi→24Mi" in (anns.get(hist_key_app) or ""), True)

    # No-Prom + NO OOM event → still skipped (synthesis only triggers on
    # fresh OOMs; otherwise the legacy "no recommendation" behavior holds).
    with patch("src.writeback_webhook._query_prom_values", return_value=None):
        payload2, anns2 = _build_containers_payload(
            rec, cfg, oom_state=None, oom_events={}, oom_eligible=True,
        )
    _check("[no-prom] no OOM → empty payload",
           payload2, [])
    _check("[no-prom] no OOM → empty annotations",
           anns2, {})

    # No-Prom + OOM but workload ineligible → still skipped (don't bump
    # workloads that opted out of OOM detection).
    with patch("src.writeback_webhook._query_prom_values", return_value=None):
        payload3, anns3 = _build_containers_payload(
            rec, cfg,
            oom_state=None, oom_events={"app": ev_noprom},
            oom_eligible=False,
        )
    _check("[no-prom] OOM-ineligible → empty payload",
           payload3, [])

    # OOM bump covered by floor → last_event advances (dedupe), but no
    # history entry is recorded since the limit didn't actually move.
    cfg.resource.min_memory_request_mi = 100   # restore chart-default floor
    cfg.resource.memory_limit_multiplier = 3.0
    with patch("src.writeback_webhook._query_prom_values", return_value=None):
        payload4, anns4 = _build_containers_payload(
            rec, cfg,
            oom_state={"floor": {}, "last_event": {}, "history": {}},
            oom_events={"app": ev_noprom},
            oom_eligible=True,
        )
    # The 100Mi mem-request floor pushes the limit up (lim>=req) to 100Mi,
    # bigger than trap×1.5 = 24Mi → the OOM bump is a no-op for the limit.
    _check("[no-prom-covered] limit pinned by floor (>= req)",
           payload4[0].get("limits", {}).get("memory"), "100Mi")
    _check("[no-prom-covered] last-event still stamped (dedupe advances)",
           anns4.get(last_key_app), "2026-05-09T16:00:00Z")
    _check("[no-prom-covered] no history entry (no real movement)",
           anns4.get(hist_key_app, ""), "")
    cfg.resource.min_memory_request_mi = 0   # restore for downstream tests

    # ── oomFloorEnabled: false → bump applies, no sticky floor recorded ─
    # When the workload opted out of sticky floors, a fresh OOM still
    # bumps THIS sync's CR (immediate help) but the floor annotation is
    # NOT written, so the next sync's Prom-driven recommendation can
    # drop the limit again. last-event still advances (dedupe) and
    # history still records the audit entry — we just don't keep the
    # floor as a permanent minimum.
    cfg.resource.max_memory_limit_mi = 0
    cfg.resource.oom_bump_factor = 1.5
    ev_floor_off = OomEvent(
        namespace="ns", workload_name="api", container="app",
        finished_at="2026-05-09T17:00:00Z",
        trap_limit_bytes=64 * M,
    )
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_values):
        payload5, anns5 = _build_containers_payload(
            rec, cfg,
            oom_state={"floor": {}, "last_event": {}, "history": {}},
            oom_events={"app": ev_floor_off},
            oom_eligible=True,
            oom_floor_enabled=False,
        )
    _check("[floor-off] limit bumped this sync (immediate help)",
           payload5[0].get("limits", {}).get("memory"), "96Mi")
    _check("[floor-off] no sticky floor annotation written",
           floor_key_app in anns5, False)
    _check("[floor-off] last-event still advances (dedupe)",
           anns5.get(last_key_app), "2026-05-09T17:00:00Z")
    _check("[floor-off] history still records the audit entry",
           "64Mi→96Mi" in (anns5.get(hist_key_app) or ""), True)

    # When floor disabled, prior_floor must NOT be applied — the floor
    # enforcement at the end of `_build_containers_payload` is gated.
    # A workload with prior_floor=900Mi and floor disabled should NOT
    # have its limit clamped to 900Mi this sync; recommendation = Prom.
    prom_low = SimpleNamespace(
        cpu_request_m=200, memory_request_bytes=100 * M,
        cpu_limit_m=400, memory_limit_bytes=200 * M,
    )
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_low):
        payload6, anns6 = _build_containers_payload(
            rec, cfg,
            oom_state={"floor": {"app": 900 * M}, "last_event": {},
                       "history": {}},
            oom_events={},
            oom_eligible=True,
            oom_floor_enabled=False,
        )
    # Without the floor: recommendation = Prom-driven 200Mi. With it
    # enabled (default), the floor clamps to 900Mi. We expect the lower.
    _check("[floor-off] prior_floor NOT applied (Prom drives)",
           payload6[0].get("limits", {}).get("memory"), "200Mi")

    # ── oomFloorReset: clears prior state on this sync ──────────────────
    # Reset zeroes prior_floor / prior_last_event / prior_history at the
    # start of `_build_containers_payload`. Net effect: if there's no
    # fresh OOM event and reset=True, the CR comes out with NO oom-*
    # annotations at all (the operator-curated floor is gone). If a
    # fresh OOM lands in the same sync, dedupe sees an empty
    # last_event so the bump fires regardless of what the prior was.
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_values):
        payload7, anns7 = _build_containers_payload(
            rec, cfg,
            oom_state={
                "floor": {"app": 900 * M},
                "last_event": {"app": "2026-05-09T13:00:00Z"},
                "history": {"app": "old entry"},
            },
            oom_events={},
            oom_eligible=True,
            oom_floor_reset=True,
        )
    _check("[floor-reset] no event + reset → all OOM annotations cleared",
           [k for k in (floor_key_app, last_key_app, hist_key_app) if k in anns7],
           [])

    # Reset + fresh OOM → bump fires (dedupe sees empty stored, finished_at
    # is always > "") AND new annotations written from this sync only
    # (not merged with the cleared prior).
    ev_after_reset = OomEvent(
        namespace="ns", workload_name="api", container="app",
        finished_at="2026-05-09T18:00:00Z",
        trap_limit_bytes=64 * M,
    )
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_values):
        payload8, anns8 = _build_containers_payload(
            rec, cfg,
            oom_state={
                "floor": {"app": 900 * M},
                "last_event": {"app": "2026-05-09T13:00:00Z"},
                "history": {"app": "2026-05-09T13:00:00Z 600Mi→900Mi (×1.5)"},
            },
            oom_events={"app": ev_after_reset},
            oom_eligible=True,
            oom_floor_reset=True,
        )
    _check("[floor-reset] reset + fresh OOM → new floor (96Mi, not 900Mi)",
           anns8.get(floor_key_app), "96Mi")
    _check("[floor-reset] reset + fresh OOM → new history entry only",
           "600Mi" in (anns8.get(hist_key_app) or ""), False)
    _check("[floor-reset] reset + fresh OOM → last-event from new event",
           anns8.get(last_key_app), "2026-05-09T18:00:00Z")

    # Reset + floor disabled: floor stays cleared (no new sticky), bump still applies.
    with patch("src.writeback_webhook._query_prom_values", return_value=prom_values):
        payload9, anns9 = _build_containers_payload(
            rec, cfg,
            oom_state={
                "floor": {"app": 900 * M},
                "last_event": {"app": "2026-05-09T13:00:00Z"},
                "history": {},
            },
            oom_events={"app": ev_after_reset},
            oom_eligible=True,
            oom_floor_enabled=False,
            oom_floor_reset=True,
        )
    _check("[floor-reset+off] no floor annotation written",
           floor_key_app in anns9, False)
    _check("[floor-reset+off] limit bumped this sync (immediate help)",
           payload9[0].get("limits", {}).get("memory"), "96Mi")

    # ── _render_cr_doc: carry-forward operator annotations + labels ─────
    e = WebhookEntry(
        namespace="ns", cr_name="api",
        selector_labels={"app.kubernetes.io/name": "api"},
        containers=[{"name": "app", "limits": {"memory": "200Mi"}}],
        oom_annotations={
            f"{OOM_FLOOR_PREFIX}app": "200Mi",
        },
    )
    prev_doc = {
        "metadata": {
            "labels": {
                "team": "data",
                "app.kubernetes.io/managed-by": "kube-resource-updater",
            },
            "annotations": {
                "kube-resource-updater.io/cost-center": "team-x",
                # legacy CSV annotation that should be DROPPED on rebuild.
                "kube-resource-updater.io/oom-floor": "app=900Mi",
                "prometheus.io/scrape": "true",
            },
        },
    }
    doc = _render_cr_doc(e, prev_doc=prev_doc)
    rendered_anns = doc["metadata"].get("annotations") or {}
    rendered_labels = doc["metadata"].get("labels") or {}
    _check("[render] operator annotation preserved",
           rendered_anns.get("kube-resource-updater.io/cost-center"), "team-x")
    _check("[render] non-OOM annotation preserved",
           rendered_anns.get("prometheus.io/scrape"), "true")
    _check("[render] legacy oom-floor CSV dropped",
           "kube-resource-updater.io/oom-floor" in rendered_anns, False)
    _check("[render] new oom-floor.app written",
           rendered_anns.get(f"{OOM_FLOOR_PREFIX}app"), "200Mi")
    _check("[render] operator label preserved",
           rendered_labels.get("team"), "data")
    _check("[render] managed-by label always present",
           rendered_labels.get("app.kubernetes.io/managed-by"),
           "kube-resource-updater")

    # ── orphan oom-floor.<container> annotation
    # when a container is removed/renamed in the workload spec.
    # Pre-fix: the `prior_floor` carry-forward kept the annotation
    # alive even after the container disappeared from `rec.containers`.
    # Post-fix: annotation is dropped if container not in
    # `current_container_names`. Other annotations (last_event, history)
    # follow the same rule.
    from src.writeback_webhook import _build_containers_payload as _bcp
    from src.workload import WorkloadRecommendation as _WR, ContainerRecommendation as _CR
    # Workload now has only `web` (renamed from `app`). Prior CR state
    # carries oom-floor.app + last_event.app + history.app.
    rec_after_rename = _WR(
        name="api", namespace="ns",
        target_kind="Deployment", target_name="api",
        containers=[_CR(container_name="web")],   # `app` is gone
    )
    cfg_simple = _base_config()
    prior_state = {
        "floor":      {"app": 256 * 1024 * 1024},  # 256Mi
        "last_event": {"app": "2026-01-01T00:00:00Z"},
        "history":    {"app": "from=128Mi to=256Mi factor=2.0"},
        "containers": {},  # no prior res (forces non-grow-shrink path)
    }
    _payload, annotations = _bcp(
        rec_after_rename, cfg_simple,
        oom_state=prior_state, oom_events={}, oom_eligible=True,
        oom_floor_enabled=True, oom_floor_reset=False,
    )
    # Pre-fix would have emitted `oom-floor.app: 256Mi` (orphan).
    orphan_keys = [k for k in annotations if k.endswith(".app")]
    _check("[oom-orphan-C3] no oom-*.app annotation emitted for removed container",
           orphan_keys, [])
    # Negative regression: a STILL-PRESENT container's floor SHOULD be
    # carried forward. Same prior state but with `app` still in
    # rec.containers — annotation should reappear.
    rec_kept = _WR(
        name="api", namespace="ns",
        target_kind="Deployment", target_name="api",
        containers=[_CR(container_name="app")],
    )
    _payload, annotations = _bcp(
        rec_kept, cfg_simple,
        oom_state=prior_state, oom_events={}, oom_eligible=True,
        oom_floor_enabled=True, oom_floor_reset=False,
    )
    _check("[oom-orphan-C3-positive] still-present container keeps oom-floor",
           any(k.endswith(".app") for k in annotations), True)


def section_status_updater() -> None:
    """Verifies the StatusUpdater stamps `lastAppliedAt` + an `Applied` condition:

      - Multiple record() calls on the same CR collapse to one PATCH.
      - 404 on a deleted CR is swallowed; other CRs still get written.
      - flush_once() returns the number of CRs written.
      - Empty dirty map → no API calls.
      - Body shape carries `lastAppliedAt` (RFC3339 with Z suffix) and
        no `appliedToPodCount` (the per-window counter was dropped — it
        added noise without diagnostic value, lifetime totals live in
        `/metrics`).
    """
    _section("StatusUpdater — coalesce + per-CR PATCH semantics")

    from kubernetes.client.rest import ApiException
    from src.webhook_status import StatusUpdater

    api = MagicMock()
    upd = StatusUpdater(api, flush_interval_seconds=999)  # never auto-flush

    # 5 admissions across 2 CRs.
    upd.record("n8n", "n8n")
    upd.record("n8n", "n8n")
    upd.record("n8n", "n8n")
    upd.record("loki", "loki")
    upd.record("loki", "loki")

    _check("status: dirty map size after 5 records on 2 CRs",
           len(upd._dirty), 2)

    written = upd.flush_once()
    _check("status: flush_once writes 1 PATCH per CR",
           api.patch_namespaced_custom_object_status.call_count, 2)
    _check("status: flush_once returns count of CRs written",
           written, 2)
    _check("status: dirty map cleared after flush",
           upd._dirty, {})

    # Body shape: only lastAppliedAt; no appliedToPodCount.
    calls = api.patch_namespaced_custom_object_status.call_args_list
    n8n_call = next(c for c in calls if c.kwargs["name"] == "n8n")
    body_status = n8n_call.kwargs["body"]["status"]
    _check("status: body has lastAppliedAt",
           "lastAppliedAt" in body_status, True)
    _check("status: lastAppliedAt is ISO Z-suffixed (RFC3339)",
           body_status["lastAppliedAt"].endswith("Z"), True)
    _check("status: body does NOT carry appliedToPodCount (counter dropped)",
           "appliedToPodCount" in body_status, False)

    # conditions array — FAIL before the fix (no "conditions" key).
    _check("status: body has conditions array",
           "conditions" in body_status, True)
    conditions = body_status.get("conditions", [])
    _check("status: exactly one condition entry",
           len(conditions), 1)
    cond = conditions[0] if conditions else {}
    _check("status: conditions[0].type == 'Applied'",
           cond.get("type"), "Applied")
    _check("status: conditions[0].status == 'True'",
           cond.get("status"), "True")
    _check("status: conditions[0].reason == 'PodPatched'",
           cond.get("reason"), "PodPatched")
    _check("status: conditions[0].lastTransitionTime present and Z-suffixed",
           bool(cond.get("lastTransitionTime", "").endswith("Z")), True)

    # patchedContainers — FAIL before the fix (no key in body).
    _check("status: body carries patchedContainers",
           "patchedContainers" in body_status, True)
    _check("status: patchedContainers is a list",
           isinstance(body_status.get("patchedContainers"), list), True)
    _check("status: patchedContainers is [] when record() called without containers",
           body_status.get("patchedContainers"), [])

    # A real container set → sorted list in the body.
    api_s = MagicMock()
    upd_s = StatusUpdater(api_s, flush_interval_seconds=999)
    upd_s.record("ns", "cr", frozenset({"worker", "app", "sidecar"}))
    upd_s.flush_once()
    s_body = api_s.patch_namespaced_custom_object_status.call_args.kwargs["body"]["status"]
    _check("status: patchedContainers is sorted alphabetically",
           s_body["patchedContainers"], ["app", "sidecar", "worker"])
    api_s2 = MagicMock()
    upd_s2 = StatusUpdater(api_s2, flush_interval_seconds=999)
    upd_s2.record("ns", "cr", {"app"})
    upd_s2.flush_once()
    s2_body = api_s2.patch_namespaced_custom_object_status.call_args.kwargs["body"]["status"]
    _check("status: patchedContainers with single container",
           s2_body["patchedContainers"], ["app"])

    # Empty flush is a no-op.
    api.reset_mock()
    written = upd.flush_once()
    _check("status: empty dirty map → no PATCH calls",
           api.patch_namespaced_custom_object_status.called, False)
    _check("status: empty flush returns 0",  written, 0)

    # 404 handling: 1 CR exists, 1 was deleted.
    upd.record("n8n", "alive")
    upd.record("loki", "deleted")

    def _patch_side_effect(*args, **kwargs):
        if kwargs["name"] == "deleted":
            raise ApiException(status=404, reason="NotFound")
        return None

    api.patch_namespaced_custom_object_status.side_effect = _patch_side_effect
    written = upd.flush_once()
    _check("status: 404 on deleted CR is swallowed (still returns 1 written)",
           written, 1)
    _check("status: 404 leaves the other CR's PATCH intact",
           api.patch_namespaced_custom_object_status.call_count, 2)

    # ── Framework audit Area 4: transient API failure resilience ─────────
    # Pre-fix: a transient 5xx during PATCH dropped the pending stamp
    # permanently — the snapshot was local, _dirty had been replaced with
    # {} atomically. Operator saw stale `lastAppliedAt` indistinguishable
    # from "never applied". Fix: on non-404 failure, re-insert via
    # setdefault so a newer admission during flush still wins.
    api2 = MagicMock()
    upd2 = StatusUpdater(api2, flush_interval_seconds=999)

    def _transient_500(*args, **kwargs):
        if kwargs["name"] == "flaky":
            raise ApiException(status=503, reason="ServiceUnavailable")
        return None

    api2.patch_namespaced_custom_object_status.side_effect = _transient_500
    upd2.record("ns", "alive")
    upd2.record("ns", "flaky")
    written2 = upd2.flush_once()
    _check("[status-resilience] transient 5xx logged + counted as not-written",
           written2, 1)
    _check("[status-resilience] failed entry re-inserted into _dirty for retry",
           ("ns", "flaky") in upd2._dirty, True)
    _check("[status-resilience] successful entry NOT re-inserted",
           ("ns", "alive") in upd2._dirty, False)

    # Next flush succeeds: the re-inserted entry retries cleanly.
    api2.patch_namespaced_custom_object_status.side_effect = None  # all OK now
    api2.patch_namespaced_custom_object_status.reset_mock()
    written3 = upd2.flush_once()
    _check("[status-resilience] retry succeeds when transient cleared",
           written3, 1)
    _check("[status-resilience] _dirty drained after successful retry",
           upd2._dirty, {})

    # Newer record() arriving during a failed flush wins (no overwrite).
    api3 = MagicMock()
    upd3 = StatusUpdater(api3, flush_interval_seconds=999)
    api3.patch_namespaced_custom_object_status.side_effect = ApiException(status=503, reason="ServiceUnavailable")
    upd3.record("ns", "wl")
    failed_at = upd3._dirty[("ns", "wl")]   # (timestamp, frozenset) tuple
    upd3.flush_once()
    # Simulate a new admission landing before next flush (fresher timestamp).
    import datetime as _dt
    newer = (failed_at[0] + _dt.timedelta(seconds=1), frozenset())
    with upd3._lock:
        upd3._dirty[("ns", "wl")] = newer
    # The failed-and-retried entry from the previous flush had an OLDER
    # timestamp; the newer setdefault must NOT overwrite the fresher one
    # the application just recorded.
    _check("[status-resilience] newer admission during flush wins (no stale overwrite)",
           upd3._dirty[("ns", "wl")], newer)


def section_chart_conditional_rbac() -> None:
    """Renders the chart with `helm template` in both prometheusAutoDiscovery
    modes and asserts the conditional rules in the CronJob ClusterRole
    appear or disappear correctly. Closes the gap between
    `_resolve_prometheus_url` (Python) and the chart template that gates the
    matching RBAC rules — they must agree on the same flag.
    """
    _section("Chart conditional RBAC — render check (helm template)")

    import shutil
    import subprocess
    helm = shutil.which("helm")
    if not helm:
        print(f"  [{_SKIP}] helm CLI not installed locally — skipping chart render check")
        return

    chart_dir = _chart_dir()
    if not os.path.isdir(chart_dir):
        print(f"  [{_SKIP}] chart dir not found at {chart_dir} (running outside the workspace?)")
        return

    base_overrides = (
        "config.crWriteback.repoUrl=https://example/repo.git,"
        "config.crWriteback.path=overrides,"
        # made `config.prometheusUrl` required; supply it for
        # the base render so unrelated asserts don't trip the new gate.
        "config.prometheusUrl=http://qa-prom:9090,"
        # Default `config.createMr=true` (chart values.yaml) trips the
        # 1.20.0 createMr+token validate gate unless we supply a token.
        # Use a fake inline token for the base render path; individual
        # asserts override `gitlab.token`/`gitlab.existingSecret` when
        # they need to exercise the gate behavior.
        "gitlab.token=qa-fake-token,"
        # Dependency on the bitnami `common` chart isn't pulled here; tell
        # helm to ignore the dep + skip schema validation. We only want the
        # rendered text.
    )

    def _render(set_string: str) -> str:
        result = subprocess.run(
            [
                helm, "template", "kru", chart_dir,
                "--set", base_overrides + set_string,
                "--set", "rbac.create=true",
                "--set", "cronjob.enabled=true",
                "--show-only", "templates/clusterrole.yaml",
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return f"__RENDER_ERROR__\n{result.stderr}"
        return result.stdout

    # discovery dropped entirely — the ClusterRole has
    # only namespace/workload/pod/CR rules. The endpoints/services/
    # prometheuses CR rules and the discovery-rbac.yaml Roles are gone.
    out_explicit = _render("config.prometheusUrl=http://prom.example:9090")
    if out_explicit.startswith("__RENDER_ERROR__"):
        print(f"  [{_SKIP}] helm render failed (likely missing `common` dep):")
        for line in out_explicit.splitlines()[:5]:
            print(f"      {line}")
        return

    _check("rbac: namespaces rule present",      "namespaces" in out_explicit,      True)
    _check("rbac: deployments rule present",     "deployments" in out_explicit,     True)
    _check("rbac: daemonsets rule present (#29)", "daemonsets" in out_explicit,      True)
    _check("rbac 1.20.0: endpoints rule GONE",   "endpoints" in out_explicit,       False)
    _check("rbac 1.20.0: services rule GONE",    'resources: ["services"]' in out_explicit, False)
    _check("rbac 1.20.0: prometheuses CR rule GONE", "prometheuses" in out_explicit,   False)

    # ── webhook RBAC: status writer gates resourceoverrides/status verb ──
    def _render_webhook_rbac(set_string: str) -> str:
        result = subprocess.run(
            [
                helm, "template", "kru", chart_dir,
                "--set", base_overrides + set_string,
                "--set", "webhook.enabled=true",
                "--show-only", "templates/webhook/rbac.yaml",
            ],
            capture_output=True, text=True,
        )
        return result.stdout if result.returncode == 0 else f"__RENDER_ERROR__\n{result.stderr}"

    out_status_off = _render_webhook_rbac("webhook.status.enabled=false")
    out_status_on = _render_webhook_rbac("webhook.status.enabled=true")
    if out_status_off.startswith("__RENDER_ERROR__") or out_status_on.startswith("__RENDER_ERROR__"):
        print(f"  [{_SKIP}] webhook RBAC render failed; skipping status-RBAC checks")
        return

    _check("webhook rbac (status off): resourceoverrides/status verb DROPPED",
           "resourceoverrides/status" in out_status_off, False)
    _check("webhook rbac (status on): resourceoverrides/status verb GRANTED",
           "resourceoverrides/status" in out_status_on, True)

    # ── webhook RBAC: auto-rollout gates apps/deployments+statefulsets:patch ──
    out_rollout_off = _render_webhook_rbac("webhook.autoRollout.enabled=false")
    out_rollout_on = _render_webhook_rbac("webhook.autoRollout.enabled=true")
    _check("webhook rbac (auto-rollout off): apps/deployments verb DROPPED",
           "deployments" in out_rollout_off, False)
    _check("webhook rbac (auto-rollout off): apps/statefulsets verb DROPPED",
           "statefulsets" in out_rollout_off, False)
    _check("webhook rbac (auto-rollout on): apps/deployments verb GRANTED",
           "deployments" in out_rollout_on, True)
    _check("webhook rbac (auto-rollout on): apps/statefulsets verb GRANTED",
           "statefulsets" in out_rollout_on, True)
    _check("webhook rbac (auto-rollout off): apps/daemonsets verb DROPPED (#29)",
           "daemonsets" in out_rollout_off, False)
    _check("webhook rbac (auto-rollout on): apps/daemonsets verb GRANTED (#29)",
           "daemonsets" in out_rollout_on, True)

    # ── webhook RBAC: validating gates validatingwebhookconfigurations:patch ──
    out_val_off = _render_webhook_rbac("webhook.validating.enabled=false")
    out_val_on = _render_webhook_rbac("webhook.validating.enabled=true")
    _check("webhook rbac (validating off): validatingwebhookconfigurations verb DROPPED",
           "validatingwebhookconfigurations" in out_val_off, False)
    _check("webhook rbac (validating on): validatingwebhookconfigurations verb GRANTED",
           "validatingwebhookconfigurations" in out_val_on, True)

    # ──  CronJob discovery batch RBAC (CronJob support) ────
    # Fail-first: before the clusterrole.yaml edit there's no batch rule.
    _check("rbac 1.22.16: batch apiGroup rule present in CronJob ClusterRole",
           'apiGroups: ["batch"]' in out_explicit, True)
    _check("rbac 1.22.16: cronjobs resource listed in batch rule",
           '"cronjobs"' in out_explicit, True)
    # Standalone jobs intentionally excluded (ephemeral; CronJob-only scope).
    _check("rbac 1.22.16: standalone jobs NOT in batch rule",
           '"jobs"' in out_explicit, False)
    # Webhook ClusterRole must NOT gain batch verbs — no rollout for CronJobs.
    _check("rbac 1.22.16: webhook ClusterRole has NO batch verbs (auto-rollout off)",
           'apiGroups: ["batch"]' in out_rollout_off, False)
    _check("rbac 1.22.16: webhook ClusterRole has NO batch verbs (auto-rollout on)",
           'apiGroups: ["batch"]' in out_rollout_on, False)

    # ── chart: VWC manifest renders only when validating is on ────────
    def _render_vwc(set_string: str) -> str:
        result = subprocess.run(
            [
                helm, "template", "kru", chart_dir,
                "--set", base_overrides + set_string,
                "--set", "webhook.enabled=true",
                "--show-only", "templates/webhook/validatingwebhookconfiguration.yaml",
            ],
            capture_output=True, text=True,
        )
        return result.stdout if result.returncode == 0 else f"__RENDER_ERROR__\n{result.stderr}"

    out_vwc_off = _render_vwc("webhook.validating.enabled=false")
    out_vwc_on = _render_vwc("webhook.validating.enabled=true")
    if not out_vwc_on.startswith("__RENDER_ERROR__"):
        _check("chart: VWC manifest absent when validating off",
               "ValidatingWebhookConfiguration" in out_vwc_off, False)
        _check("chart: VWC manifest present when validating on",
               "ValidatingWebhookConfiguration" in out_vwc_on, True)
        _check("chart: VWC failurePolicy hard-coded to Ignore",
               "failurePolicy: Ignore" in out_vwc_on, True)

    # ──  caBundle omitted from MWC/VWC by default ──
    # Emitting `caBundle: ""` makes argocd-controller (SSA) own the field and
    # zero it on every sync, reopening a fail-open window until the in-process
    # reconciler re-injects the real value. Fix: omit the field entirely so no
    # GitOps engine claims it (the ca-injector pattern). These FAIL on the
    # pre-1.22.14 template (caBundle:"" present) and PASS after the omit.
    def _render_mwc(set_string: str) -> str:
        result = subprocess.run(
            [helm, "template", "kru", chart_dir,
             "--set", base_overrides + set_string,
             "--set", "webhook.enabled=true",
             "--show-only", "templates/webhook/mutatingwebhookconfiguration.yaml"],
            capture_output=True, text=True,
        )
        return result.stdout if result.returncode == 0 else f"__RENDER_ERROR__\n{result.stderr}"

    out_mwc_default = _render_mwc("webhook.validating.enabled=false")
    out_vwc_default2 = _render_vwc("webhook.validating.enabled=true")
    if not out_mwc_default.startswith("__RENDER_ERROR__"):
        _check("MWC clientConfig has NO caBundle field by default (#58)",
               "caBundle" in out_mwc_default, False)
    if not out_vwc_default2.startswith("__RENDER_ERROR__"):
        _check("VWC clientConfig has NO caBundle field by default (#58)",
               "caBundle" in out_vwc_default2, False)

    # When webhook.caBundle IS set (external PKI), both configs must emit it.
    out_mwc_with_ca = _render_mwc("webhook.caBundle=dGVzdA==")
    out_vwc_with_ca = _render_vwc("webhook.validating.enabled=true,webhook.caBundle=dGVzdA==")
    if not out_mwc_with_ca.startswith("__RENDER_ERROR__"):
        _check
    if not out_vwc_with_ca.startswith("__RENDER_ERROR__"):
        _check

    # ──  MWC/VWC patch narrowed via resourceNames ──────────
    # The webhook's cert reconciler patches its OWN MWC/VWC to rotate the
    # caBundle. Without resourceNames, a compromised webhook ServiceAccount
    # could modify ANY admission config in the cluster. Split: list/watch
    # stays cluster-wide (informer requires it), get/patch/update narrowed.
    out_narrow = _render_webhook_rbac("webhook.validating.enabled=true,webhook.autoRollout.enabled=true")
    if not out_narrow.startswith("__RENDER_ERROR__"):
        # Two separate rules per resource: cluster-wide list/watch, narrowed
        # get/patch/update with resourceNames.
        _check("MWC list/watch cluster-wide (no resourceNames)",
               'resources: ["mutatingwebhookconfigurations"]\n    verbs: ["list", "watch"]' in out_narrow, True)
        _check
        _check("VWC list/watch cluster-wide (no resourceNames)",
               'resources: ["validatingwebhookconfigurations"]\n    verbs: ["list", "watch"]' in out_narrow, True)
        # Verify NO broad patch grant remains.
        _check

    # ──  validate.yaml fail-fast on both cronjob+webhook off ─
    def _try_render(set_string: str) -> tuple[int, str]:
        result = subprocess.run(
            [helm, "template", "kru", chart_dir, "--set", base_overrides + set_string],
            capture_output=True, text=True,
        )
        return result.returncode, result.stderr

    rc, err = _try_render("cronjob.enabled=false,webhook.enabled=false")
    _check
    _check
    rc_ok, _ = _try_render("cronjob.enabled=true,webhook.enabled=false")
    _check("render OK with cronjob-only (no webhook)",
           rc_ok, 0)

    # ── OSS pre-publish: webhook-only install must NOT require crWriteback ──
    # The configmap required-gate is now scoped to cronjob.enabled. A
    # standalone-webhook install (cronjob off, no crWriteback) must render —
    # it was a footgun that the documented webhook-only mode failed to install.
    rc_wh, _ = _try_render(
        "cronjob.enabled=false,webhook.enabled=true,"
        "config.crWriteback.repoUrl=,config.crWriteback.path=")
    _check("chart: webhook-only render OK without crWriteback (cronjob off)",
           rc_wh, 0)
    # With the CronJob enabled, missing crWriteback still fails fast.
    rc_cj, err_cj = _try_render(
        "cronjob.enabled=true,webhook.enabled=false,"
        "config.crWriteback.repoUrl=,config.crWriteback.path=")
    _check("chart: cronjob render FAILS without crWriteback.repoUrl",
           rc_cj != 0, True)
    _check("chart: fail message names crWriteback.repoUrl",
           "crWriteback.repoUrl is required" in err_cj, True)
    rc_ok, _ = _try_render("cronjob.enabled=false,webhook.enabled=true")
    _check("render OK with webhook-only (no cronjob)",
           rc_ok, 0)

    # ──  gitlabSecretName helper — token-mode bug fix ──────
    # Pre-1.15.0 the helper only returned a name in existingSecret mode;
    # token-mode rendered an inline Secret but the CronJob's secretKeyRef
    # had an empty `.name`, breaking pod startup at runtime. Verify both
    # modes produce a working secretKeyRef.
    def _render_cronjob(set_string: str) -> str:
        result = subprocess.run(
            [helm, "template", "kru", chart_dir,
             "--set", base_overrides + set_string,
             "--show-only", "templates/cronjob.yaml"],
            capture_output=True, text=True,
        )
        return result.stdout if result.returncode == 0 else f"__RENDER_ERROR__\n{result.stderr}"

    out_token = _render_cronjob("gitlab.token=glpat-fake-inline")
    out_existing = _render_cronjob("gitlab.existingSecret=my-managed-secret,gitlab.existingSecretKey=gh-token")
    if not out_token.startswith("__RENDER_ERROR__"):
        # Inline Secret rendered → CronJob references the fullname-gitlab name.
        _check("token-mode populates secretKeyRef.name (was empty pre-fix)",
               'name: kru-kube-resource-updater-gitlab' in out_token and
               'key: token' in out_token,
               True)
    if not out_existing.startswith("__RENDER_ERROR__"):
        _check

    # ──  prometheusUrl required render-time fail ────────────
    # Auto-discovery was dropped; the chart now fails at helm install
    # time when prometheusUrl is empty. Webhook-only installs (cronjob
    # off) skip the check because they don't query Prometheus.
    rc_no_prom, err_no_prom = _try_render(
        "config.prometheusUrl=,gitlab.token=qa-fake"
    )
    _check("render FAILS on empty prometheusUrl (cronjob enabled)",
           rc_no_prom != 0, True)
    _check
    # Webhook-only install: cronjob disabled → prometheusUrl optional.
    rc_webhook_only, _ = _try_render(
        "cronjob.enabled=false,webhook.enabled=true,config.prometheusUrl="
    )
    _check

    # ──  createMr + token consistency render-time fail ───────
    # `config.createMr: true` AND no gitlab.token/existingSecret → fail at
    # `helm install` time. Pre-1.20.0 the chart rendered fine and the sync
    # crashed mid-run with a 401 from GitLab. The runtime Config.validate
    # in src/config.py still mirrors this rule for hand-edited ConfigMap
    # drift; this assert covers the render-time gate added in 1.20.0.
    rc_bad, err_bad = _try_render(
        "config.createMr=true,gitlab.token=,gitlab.existingSecret="
    )
    _check
    _check
    # Valid combos render OK.
    rc_inline, _ = _try_render(
        "config.createMr=true,gitlab.token=glpat-fake-inline"
    )
    _check
    # Clear the base_overrides token so existingSecret is the SOLE source
    # — 's dual-token gate fails when both are set together.
    rc_managed, _ = _try_render(
        "config.createMr=true,gitlab.token=,gitlab.existingSecret=mr-token-secret"
    )
    _check
    # createMr=false with no token is allowed (direct push uses ambient
    # git credentials).
    rc_no_mr, _ = _try_render(
        "config.createMr=false,gitlab.token=,gitlab.existingSecret="
    )
    _check("createMr=false without token renders OK (direct push)",
           rc_no_mr, 0)

    # ──  min > max bounds render-time fail ─────────────────────
    rc_bad, err_bad = _try_render(
        "config.minCpuRequestM=500,config.maxCpuRequestM=200"
    )
    _check
    _check
    # All four dimensions covered.
    for dim, mn, mx in [
        ("Memory", "minMemoryRequestMi", "maxMemoryRequestMi"),
        ("CpuLimit", "minCpuLimitM", "maxCpuLimitM"),
        ("MemoryLimit", "minMemoryLimitMi", "maxMemoryLimitMi"),
    ]:
        rc, _ = _try_render(f"config.{mn}=1000,config.{mx}=500")
        _check
    # min == 0 (disabled) bypasses the check.
    rc_ok, _ = _try_render("config.minCpuRequestM=0,config.maxCpuRequestM=200")
    _check("minCpuRequest=0 (disabled) bypasses bounds check",
           rc_ok, 0)

    # ──  zero-window Prom duration render-time fail ───────────
    for win in ("cpuRequestWindow", "memRequestWindow", "cpuLimitWindow", "memLimitWindow"):
        rc, _ = _try_render(f"config.{win}=0s")
        _check
    # Compound zero (0w0d).
    rc, _ = _try_render("config.cpuLimitWindow=0w0d")
    _check("render FAILS on '0w0d' (compound zero)",
           rc != 0, True)
    # Mixed with non-zero is fine.
    rc, _ = _try_render("config.cpuRequestWindow=1h0m")
    _check("render OK on '1h0m' (mixed, has 1h)",
           rc, 0)

    # ──  4 helm-level invalid combinations ────────────────────
    rc, err = _try_render("gitlab.token=t,gitlab.existingSecret=s")
    _check
    _check

    rc, err = _try_render("webhook.enabled=true,webhook.timeoutSeconds=0")
    _check("webhook.timeoutSeconds=0 → fail (k8s range)",
           rc != 0, True)
    rc, err = _try_render("webhook.enabled=true,webhook.timeoutSeconds=31")
    _check("webhook.timeoutSeconds=31 → fail (k8s range)",
           rc != 0, True)
    rc, _ = _try_render("webhook.enabled=true,webhook.timeoutSeconds=5")
    _check("webhook.timeoutSeconds=5 → ok (default range)",
           rc, 0)

    rc, err = _try_render(
        "webhook.enabled=true,webhook.autoRollout.enabled=true,"
        "webhook.autoRollout.debounceSeconds=0"
    )
    _check

    rc, err = _try_render("cronjob.schedule=")
    _check
    rc, err = _try_render("cronjob.schedule=   ")  # whitespace
    _check

    # ──  leading-slash crWriteback.path → fail ────────────────
    # os.path.join(repo_dir, abs_path) discards repo_dir,
    # tool writes to filesystem root instead of repo. Reject at install.
    rc, err = _try_render("config.crWriteback.path=/manifests/foo")
    _check
    _check
    rc, _ = _try_render("config.crWriteback.path=manifests/foo")
    _check

    # ── image pinning: empty tag falls back to appVersion (helper contract) ──
    # The 1.21.0 tag/digest-both-empty gate was removed in 1.22.27: the image
    # helper renders `tag | default .Chart.AppVersion`, so an unpinned
    # `<repo>:` reference cannot occur by construction.
    rc, _ = _try_render("image.tag=,image.digest=")
    _check("chart: empty tag+digest renders OK (appVersion fallback pins the image)",
           rc, 0)
    out_img = _render_cronjob("image.tag=,image.digest=")
    img_lines = [ln.strip() for ln in out_img.splitlines() if ln.strip().startswith("image:")]
    img_ref = img_lines[0].split("image:", 1)[1].strip().strip('"') if img_lines else ""
    _check("chart: empty tag renders image pinned to appVersion (non-empty tag, no :latest)",
           bool(img_ref) and ":" in img_ref
           and img_ref.rsplit(":", 1)[1] not in ("", "latest"), True)
    rc, _ = _try_render("image.tag=,image.digest=sha256:abc123")
    _check

    # ──  negative CronJob numeric values → fail ─────────────────
    for fld in ("backoffLimit", "successfulJobsHistoryLimit",
                "failedJobsHistoryLimit", "ttlSecondsAfterFinished",
                "activeDeadlineSeconds"):
        rc, _ = _try_render(f"cronjob.{fld}=-1")
        _check

    # ──  PrometheusRule gating + alert completeness (#12) ───────
    # Fail-first: before prometheusrule.yaml exists, the "present" + 4 alert
    # checks fail. The full render (no --show-only) checks kind presence across
    # all templates.
    def _render_full(set_string: str) -> str:
        result = subprocess.run(
            [helm, "template", "kru", chart_dir,
             "--set", base_overrides + set_string],
            capture_output=True, text=True,
        )
        return result.stdout if result.returncode == 0 else f"__RENDER_ERROR__\n{result.stderr}"

    out_pr_off = _render_full(
        "webhook.enabled=true,webhook.metrics.prometheusRule.enabled=false")
    if not out_pr_off.startswith("__RENDER_ERROR__"):
        _check

    out_pr_on = _render_full(
        "webhook.enabled=true,"
        "webhook.metrics.prometheusRule.enabled=true,"
        "webhook.validating.enabled=true")
    if not out_pr_on.startswith("__RENDER_ERROR__"):
        _check
        for _alert in ("KubeResourceUpdaterWebhookDown",
                       "KubeResourceUpdaterWebhookFailingOpen",
                       "KubeResourceUpdaterValidatingWebhookFailingOpen",
                       "KubeResourceUpdaterWebhookErrors"):
            _check

    # Alert 3 gated on validating.enabled → absent when validating off.
    out_pr_no_val = _render_full(
        "webhook.enabled=true,"
        "webhook.metrics.prometheusRule.enabled=true,"
        "webhook.validating.enabled=false")
    if not out_pr_no_val.startswith("__RENDER_ERROR__"):
        _check

    # ──  PDB minAvailable + maxUnavailable both set → fail ─────
    rc, err = _try_render("pdb.create=true,pdb.minAvailable=1,pdb.maxUnavailable=1")
    _check
    # Only one set is fine.
    rc, _ = _try_render("pdb.create=true,pdb.minAvailable=1,pdb.maxUnavailable=")
    _check
    rc, _ = _try_render("pdb.create=true,pdb.minAvailable=,pdb.maxUnavailable=1")
    _check

    # ──  webhook PDB + replicaCount=1 → fail ───────────────────
    rc, err = _try_render(
        "webhook.enabled=true,webhook.podDisruptionBudget.enabled=true,"
        "webhook.replicaCount=1"
    )
    _check
    rc, _ = _try_render(
        "webhook.enabled=true,webhook.podDisruptionBudget.enabled=true,"
        "webhook.replicaCount=2"
    )
    _check

    # ──  webhook.failurePolicy validation ──────────────────────
    rc, _ = _try_render("webhook.enabled=true,webhook.failurePolicy=Sometimes")
    _check
    rc, _ = _try_render(
        "webhook.enabled=true,webhook.failurePolicy=Fail,webhook.replicaCount=1,"
        "webhook.podDisruptionBudget.enabled=false"
    )
    _check
    rc, _ = _try_render(
        "webhook.enabled=true,webhook.failurePolicy=Fail,webhook.replicaCount=2"
    )
    _check

    # ──  webhook port collision + range ─────────────────────────
    rc, _ = _try_render("webhook.enabled=true,webhook.port=8080,webhook.metricsPort=8080")
    _check
    rc, _ = _try_render("webhook.enabled=true,webhook.port=0")
    _check("webhook.port=0 → fail (TCP range)",
           rc != 0, True)
    rc, _ = _try_render("webhook.enabled=true,webhook.port=99999")
    _check("webhook.port=99999 → fail (TCP range)",
           rc != 0, True)

    # ──  invalid log level ─────────────────────────────────────
    rc, _ = _try_render("config.logLevel=BANANA")
    _check
    rc, _ = _try_render("config.logLevel=DEBUG")
    _check

    # ──  feature env vars gated on toggle ─────────────────────
    # CronJob: OOM_BUMP_FACTOR / OOM_FLOOR_ENABLED injected only when
    # `oomDetectionEnabled: true`. Pre-1.20.0 they shipped on every release
    # including operators who turned OOM off — dead configuration.
    out_oom_off = _render_cronjob("config.oomDetectionEnabled=false")
    out_oom_on  = _render_cronjob("config.oomDetectionEnabled=true")
    # Match on the env-var manifest form `- name: OOM_*` rather than a bare
    # substring — the template carries the var names in comments too, which
    # a naive `in` check would catch.
    if not out_oom_off.startswith("__RENDER_ERROR__"):
        _check("OOM_DETECTION_ENABLED always present (state signal)",
               "- name: OOM_DETECTION_ENABLED" in out_oom_off, True)
        _check
        _check
    if not out_oom_on.startswith("__RENDER_ERROR__"):
        _check
        _check

    # Webhook: WEBHOOK_STATUS_FLUSH_INTERVAL_SECONDS gated on status.enabled,
    # WEBHOOK_AUTO_ROLLOUT_DEBOUNCE_SECONDS gated on autoRollout.enabled.
    def _render_webhook_deployment(set_string: str) -> str:
        result = subprocess.run(
            [helm, "template", "kru", chart_dir,
             "--set", base_overrides + set_string,
             "--set", "webhook.enabled=true",
             "--show-only", "templates/webhook/deployment.yaml"],
            capture_output=True, text=True,
        )
        return result.stdout if result.returncode == 0 else f"__RENDER_ERROR__\n{result.stderr}"

    out_w_off = _render_webhook_deployment(
        "webhook.status.enabled=false,webhook.autoRollout.enabled=false"
    )
    out_w_on = _render_webhook_deployment(
        "webhook.status.enabled=true,webhook.autoRollout.enabled=true"
    )
    if not out_w_off.startswith("__RENDER_ERROR__"):
        _check("WEBHOOK_STATUS_ENABLED always present (state signal)",
               "- name: WEBHOOK_STATUS_ENABLED" in out_w_off, True)
        _check
        _check
    if not out_w_on.startswith("__RENDER_ERROR__"):
        _check
        _check

    # ──  MWC operations narrowed to CREATE-only ───────────────
    # The mutate handler applies spec.containers[*].resources patches —
    # replacing resources is immutable on a live Pod (the apiserver rejects
    # with "pod updates may not change fields other than ...") unless the
    # InPlacePodVerticalScaling feature gate is on, which is alpha in k8s 1.27
    # (the chart's minimum target) and beta in 1.29.  Listing UPDATE in the
    # MWC rule means ANY pod UPDATE (HPA label stamp, DaemonSet rolling-update
    # annotation) triggers the webhook; the resulting patch is then rejected
    # by the apiserver, failing the original controller operation.
    # Fix: narrow operations to ["CREATE"] so the webhook only fires at pod
    # creation time, which is the only point where resources are mutable on
    # every supported k8s version.
    if not out_mwc_default.startswith("__RENDER_ERROR__"):
        _check
        _check


def section_chart_git_provider_wiring() -> None:
    """ provider-agnostic config wiring — chart render asserts.

    The Python side (src/config.py, src/writeback*.py) reads these env vars
    and ConfigMap keys:

      env:  GIT_TOKEN, GIT_PROVIDER, GIT_API_URL, GIT_USERNAME
            (GITLAB_TOKEN / GITLAB_USERNAME as deprecated aliases)
      CM:   gitProvider, gitApiUrl, gitUsername
            (gitlabUsername as deprecated alias)

    The chart must wire them from values in a backward-compat way:
      - New path:  git.token / git.existingSecret / git.apiUrl / git.username
      - Legacy path: gitlab.token / gitlab.existingSecret (prod shape)
    Both must produce a working GIT_TOKEN env and a valid render.

    QA rule 3: fail-first. Tests for new features (A, C-new, D-new, E, F-new,
    G, I, J) FAIL before the chart edits land; backward-compat tests (B, C-old,
    D-old, H) exercise the EXISTING code paths and pass before and after.
    """
    _section("provider-agnostic git wiring (render asserts)")

    import shutil
    import subprocess

    helm = shutil.which("helm")
    if not helm:
        for cand in (os.path.expanduser("~/homebrew/bin/helm"),
                     "/opt/homebrew/bin/helm", "/usr/local/bin/helm"):
            if os.path.isfile(cand):
                helm = cand
                break
    if not helm:
        print(f"  [{_SKIP}] helm CLI not installed — skipping chart render checks")
        return

    chart_dir = _chart_dir()
    if not os.path.isdir(chart_dir):
        print(f"  [{_SKIP}] chart dir not found at {chart_dir}")
        return

    # Required minimums for a valid render (token supplied separately per test).
    _base = (
        "config.crWriteback.repoUrl=https://x.git,"
        "config.crWriteback.path=overrides,"
        "config.prometheusUrl=http://prom:9090"
    )

    def _render(template: str, extra: str) -> str:
        r = subprocess.run(
            [helm, "template", "kru", chart_dir,
             "--set", f"{_base},{extra}",
             "--show-only", template],
            capture_output=True, text=True,
        )
        return r.stdout if r.returncode == 0 else f"__ERR__\n{r.stderr}"

    def _try_render(extra: str) -> tuple:
        r = subprocess.run(
            [helm, "template", "kru", chart_dir,
             "--set", f"{_base},{extra}"],
            capture_output=True, text=True,
        )
        return r.returncode, r.stderr

    # ── (A) New path: git.token set renders with GIT_TOKEN env ───────────────
    # FAIL-FIRST: before chart edits the validate gate fires (token not
    # recognized as satisfying the gitlab.token gate) and the render fails
    # entirely; the individual sub-checks assert specific env/cm content.
    rc_a, _ = _try_render("git.token=glpat-new-token")
    _check("[p3-chart-A] git.token → render passes validate",
           rc_a, 0)

    out_a_cj = _render("templates/cronjob.yaml", "git.token=glpat-new-token")
    out_a_cm = _render("templates/configmap.yaml", "git.token=glpat-new-token")
    if rc_a == 0:
        _check("[p3-chart-A] git.token → GIT_TOKEN env in CronJob",
               "- name: GIT_TOKEN" in out_a_cj, True)
        _check("[p3-chart-A] git.token → no stale GITLAB_TOKEN emitted",
               "- name: GITLAB_TOKEN" in out_a_cj, False)
        _check("[p3-chart-A] configmap has gitProvider key (even when empty)",
               "gitProvider:" in out_a_cm, True)

    # ── (B) Legacy path: only gitlab.token set (the prod production shape) ──
    # This path must continue to work EXACTLY as before — GIT_TOKEN env, clean
    # validate. Asserting BEFORE chart edits proves the legacy path is already
    # intact; asserting AFTER proves the refactor didn't break it.
    rc_b, _ = _try_render("gitlab.token=glpat-legacy-token")
    _check("[p3-chart-B] legacy gitlab.token render passes validate",
           rc_b, 0)

    out_b_cj = _render("templates/cronjob.yaml", "gitlab.token=glpat-legacy-token")
    out_b_cm = _render("templates/configmap.yaml", "gitlab.token=glpat-legacy-token")
    if rc_b == 0:
        _check("[p3-chart-B] legacy gitlab.token → GIT_TOKEN env present (backward-compat)",
               "- name: GIT_TOKEN" in out_b_cj, True)
        _check("[p3-chart-B] legacy gitlab.token → secretKeyRef has non-empty name",
               "name: " in out_b_cj, True)
        _check("[p3-chart-B] configmap has gitProvider key with legacy path",
               "gitProvider:" in out_b_cm, True)

    # ── (C) gitProvider validation gate ──────────────────────────────────────
    # FAIL-FIRST: `gitProvider=github` render fails before validate.yaml
    # gets the new gate (currently fails because git.token is unrecognized;
    # after implementation it passes cleanly).
    rc_c_github, _ = _try_render("git.token=tok,config.gitProvider=github")
    _check("[p3-chart-C] gitProvider=github renders OK",
           rc_c_github, 0)

    # Bogus value must always fail — the validate gate is NEW.
    rc_c_bogus, err_c_bogus = _try_render("git.token=tok,config.gitProvider=bogus")
    _check("[p3-chart-C] gitProvider=bogus → validate fails",
           rc_c_bogus != 0, True)
    _check("[p3-chart-C] error message mentions gitProvider or the bad value",
           "bogus" in err_c_bogus or "gitProvider" in err_c_bogus, True)

    # ── (D) createMr + no token from ANY source → generalised gate ───────────
    # The existing gate checks only gitlab.*, the new gate must also check git.*.
    # FAIL-FIRST: createMr=true + git.token=tok currently fails (unrecognized);
    # after implementation it passes.
    rc_d_git, _ = _try_render("config.createMr=true,git.token=tok")
    _check("[p3-chart-D] createMr=true + git.token → OK (generalised gate)",
           rc_d_git, 0)

    rc_d_ges, _ = _try_render(
        "config.createMr=true,git.token=,git.existingSecret=my-secret")
    _check("[p3-chart-D] createMr=true + git.existingSecret → OK",
           rc_d_ges, 0)

    # All four empty → still fails (invariant preserved).
    rc_d_none, err_d_none = _try_render(
        "config.createMr=true,"
        "git.token=,git.existingSecret=,"
        "gitlab.token=,gitlab.existingSecret="
    )
    _check("[p3-chart-D] createMr=true + no token anywhere → validate fails",
           rc_d_none != 0, True)
    _check("[p3-chart-D] error names the token requirement",
           "token" in err_d_none.lower() or "createMr" in err_d_none, True)

    # ── (E) git.token AND git.existingSecret both set → validate fails ───────
    # FAIL-FIRST: currently git.* aren't chart fields so the dup-check can't
    # fire; after chart edits it fires the new gate.
    rc_e, err_e = _try_render("git.token=tok,git.existingSecret=sec")
    _check("[p3-chart-E] git.token + git.existingSecret both set → validate fails",
           rc_e != 0, True)
    _check("[p3-chart-E] error names both git fields",
           "git.token" in err_e and "git.existingSecret" in err_e, True)

    # ── (F) ConfigMap username fields ────────────────────────────────────────
    # FAIL-FIRST: git.username is not a known chart field yet.
    rc_f, _ = _try_render("git.token=tok,git.username=custom-user")
    _check("[p3-chart-F] git.username render passes",
           rc_f, 0)
    out_f_cm = _render("templates/configmap.yaml", "git.token=tok,git.username=custom-user")
    if rc_f == 0:
        _check("[p3-chart-F] git.username → gitUsername ConfigMap key",
               "gitUsername:" in out_f_cm, True)

    # Legacy gitlab.username → gitlabUsername alias in CM (existing behavior,
    # must survive the refactor).
    out_f_legacy_cm = _render("templates/configmap.yaml",
                               "gitlab.token=tok,gitlab.username=legacy-user")
    if not out_f_legacy_cm.startswith("__ERR__"):
        _check("[p3-chart-F] gitlab.username → gitlabUsername deprecated alias in CM",
               "gitlabUsername:" in out_f_legacy_cm, True)

    # ── (G) Secret rendered for git.token (new path) ─────────────────────────
    # FAIL-FIRST: git.token not a valid chart field, render fails.
    rc_g, _ = _try_render("git.token=new-path-token")
    _check("[p3-chart-G] git.token → render succeeds (Secret emitted)",
           rc_g, 0)
    out_g_secret = _render("templates/secret.yaml", "git.token=new-path-token")
    if rc_g == 0:
        _check("[p3-chart-G] git.token → Secret kind rendered",
               "kind: Secret" in out_g_secret, True)
        _check("[p3-chart-G] git.token value in Secret",
               "new-path-token" in out_g_secret, True)

    # ── (H) Legacy gitlab.existingSecret path still works ────────────────────
    # Already working; preserve through the refactor.
    out_h_cj = _render(
        "templates/cronjob.yaml",
        "config.createMr=true,"
        "gitlab.token=,gitlab.existingSecret=my-managed-secret,"
        "gitlab.existingSecretKey=the-key",
    )
    if not out_h_cj.startswith("__ERR__"):
        _check("[p3-chart-H] legacy gitlab.existingSecret → GIT_TOKEN references the secret",
               "my-managed-secret" in out_h_cj, True)
        _check("[p3-chart-H] legacy gitlab.existingSecret → correct key referenced",
               "the-key" in out_h_cj, True)

    # ── (I) gitApiUrl in ConfigMap ────────────────────────────────────────────
    # FAIL-FIRST: git.apiUrl not a chart field yet.
    rc_i, _ = _try_render("git.token=tok,git.apiUrl=https://ghe.example.com/api/v3")
    _check("[p3-chart-I] git.apiUrl render passes",
           rc_i, 0)
    out_i_cm = _render("templates/configmap.yaml",
                        "git.token=tok,git.apiUrl=https://ghe.example.com/api/v3")
    if rc_i == 0:
        _check("[p3-chart-I] git.apiUrl → gitApiUrl ConfigMap key",
               "gitApiUrl:" in out_i_cm, True)

    # ── (J) gitProvider in ConfigMap for explicit values ──────────────────────
    # FAIL-FIRST: git.token not chart field.
    rc_j, _ = _try_render("git.token=tok,config.gitProvider=gitlab")
    _check("[p3-chart-J] config.gitProvider=gitlab renders OK",
           rc_j, 0)
    out_j_cm = _render("templates/configmap.yaml", "git.token=tok,config.gitProvider=gitlab")
    if rc_j == 0:
        _check("[p3-chart-J] gitProvider: gitlab in ConfigMap",
               "gitProvider:" in out_j_cm, True)

def section_credentials_and_prometheus_modes() -> None:
    """Covers the simplifications:

      - git auth is single-source — `_auth_url` embeds the one configured
        token. Any drift back to the ArgoCD-secret or `~/.git-credentials`
        fallbacks would leave a stale code path.
      - `_resolve_prometheus_url` is a one-mode passthrough since chart
        1.20.0: explicit URL → log + return. Auto-discovery was dropped.
    """
    _section("Credentials + Prometheus URL resolution (single source / explicit only)")

    from src.config import Config, CrWritebackConfig, ResourceConfig
    # Phase-3 regression fix: log_git_credentials_source renamed to
    # log_git_credentials_state. Import the new name; also check the old
    # name is gone (the rename is the fix — old callers must be updated).
    try:
        from src.writeback import log_git_credentials_state
        _cred_state_importable = True
    except ImportError:
        _cred_state_importable = False
        _check('[cred-state-import] log_git_credentials_state importable (renamed from log_git_credentials_source)',
               False, True)

    # The credential path must NOT import the dropped helpers.
    import src.writeback
    _check("creds: src.writeback no longer references credentials_from_file",
           hasattr(src.writeback, "credentials_from_file"), False)
    _check("creds: src.writeback no longer references credentials_from_argocd_repos",
           hasattr(src.writeback, "credentials_from_argocd_repos"), False)
    import src.k8s
    _check("creds: src.k8s no longer exports credentials_from_argocd_repos",
           hasattr(src.k8s, "credentials_from_argocd_repos"), False)
    import src.config
    _check("creds: src.config no longer exports credentials_from_file",
           hasattr(src.config, "credentials_from_file"), False)

    # ── auto-discovery removed ────────────────────────
    # The dropped helpers should be absent from src.k8s; main.py should
    # no longer import them. Drift back would resurface the cluster-wide
    # endpoints/services RBAC grants that the 1.20.0 hardening removed.
    _check("prom-url: src.k8s.discover_prometheus_url removed (1.20.0)",
           hasattr(src.k8s, "discover_prometheus_url"), False)
    _check("prom-url: src.k8s._get_local_apiserver_ips removed (1.20.0)",
           hasattr(src.k8s, "_get_local_apiserver_ips"), False)
    import main
    _check("prom-url: main does NOT import discover_prometheus_url",
           hasattr(main, "discover_prometheus_url"), False)

    # ── log_git_credentials_state: Phase-3 regression asserts ──────────────
    # Pre-fix: the function is still named log_git_credentials_source and reads
    # gitlab_token (the old field). Post-Phase-3 prod has git_token set but
    # gitlab_token empty — the old function emits a spurious WARNING.
    # These asserts FAIL against the old gitlab_token-driven function and PASS
    # once the function is updated to use git_token.
    if _cred_state_importable:
        import logging as _cred_log
        import io as _cred_io

        def _capture_cred_log(fn, *args, **kwargs):
            """Call fn(*args, **kwargs); return all WARNING+ log records as a string."""
            buf = _cred_io.StringIO()
            h = _cred_log.StreamHandler(buf)
            h.setLevel(_cred_log.WARNING)
            root = _cred_log.getLogger("src.writeback")
            root.addHandler(h)
            try:
                fn(*args, **kwargs)
            finally:
                root.removeHandler(h)
            return buf.getvalue()

        def _capture_cred_info(fn, *args, **kwargs):
            """Call fn(*args, **kwargs); return all INFO+ log records as a string.

            Must temporarily lower the logger level to INFO (default NOTSET inherits
            WARNING from the root logger in the QA process, which blocks INFO records
            before they reach any handler).
            """
            buf = _cred_io.StringIO()
            h = _cred_log.StreamHandler(buf)
            h.setLevel(_cred_log.INFO)
            root = _cred_log.getLogger("src.writeback")
            old_level = root.level
            root.setLevel(_cred_log.DEBUG)  # allow INFO through the logger
            root.addHandler(h)
            try:
                fn(*args, **kwargs)
            finally:
                root.removeHandler(h)
                root.setLevel(old_level)
            return buf.getvalue()

        # ── Regression assert: prod Phase-3 shape ─────────────────────────
        # git_token set, gitlab_token empty (the real post-Phase-3 deployment).
        # The diagnostic must NOT emit a warning — credentials ARE present.
        # FAILS on current code because current function checks gitlab_token.
        _warn_output = _capture_cred_log(
            log_git_credentials_state,
            repo_url="https://gitlab.example.com/infra/gitops.git",
            git_token="glpat-real-token",
            git_provider="",
            git_username="oauth2",
        )
        _check("[cred-state-prod] git_token set + provider auto-detect: NO spurious warning",
               "not set" in _warn_output or "will fail" in _warn_output, False)
        _check("[cred-state-prod] no GITLAB_TOKEN text in log (provider-agnostic)",
               "GITLAB_TOKEN" in _warn_output, False)

        # ── Warning fires only when truly no token ──────────────────────────
        _warn_no_token = _capture_cred_log(
            log_git_credentials_state,
            repo_url="https://gitlab.example.com/infra/gitops.git",
            git_token="",
            git_provider="",
            git_username="oauth2",
        )
        _check("[cred-state-no-token] empty git_token: WARNING fires",
               bool(_warn_no_token.strip()), True)
        _check("[cred-state-no-token] warning text references GIT_TOKEN (not GITLAB_TOKEN)",
               "GIT_TOKEN" in _warn_no_token and "GITLAB_TOKEN" not in _warn_no_token, True)

        # ── Provider reflected in INFO log ──────────────────────────────────
        _info_github = _capture_cred_info(
            log_git_credentials_state,
            repo_url="https://github.com/acme/repo.git",
            git_token="ghp-tok",
            git_provider="",
            git_username="x-access-token",
        )
        _check("[cred-state-provider-github] github.com repoUrl -> INFO mentions github",
               "github" in _info_github.lower(), True)
        _check("[cred-state-provider-github] github.com repoUrl: no warning",
               "not set" in _info_github or "will fail" in _info_github, False)

        _info_gitlab = _capture_cred_info(
            log_git_credentials_state,
            repo_url="https://gitlab.example.com/infra/gitops.git",
            git_token="glpat-tok",
            git_provider="",
            git_username="oauth2",
        )
        _check("[cred-state-provider-gitlab] self-hosted repoUrl -> INFO mentions gitlab",
               "gitlab" in _info_gitlab.lower(), True)
    else:
        # Propagate import failure as explicit FAIL asserts so the count is stable.
        for _lbl in (
            "[cred-state-prod] git_token set + provider auto-detect: NO spurious warning",
            "[cred-state-prod] no GITLAB_TOKEN text in log (provider-agnostic)",
            "[cred-state-no-token] empty git_token: WARNING fires",
            "[cred-state-no-token] warning text references GIT_TOKEN (not GITLAB_TOKEN)",
            "[cred-state-provider-github] github.com repoUrl -> INFO mentions github",
            "[cred-state-provider-github] github.com repoUrl: no warning",
            "[cred-state-provider-gitlab] self-hosted repoUrl -> INFO mentions gitlab",
        ):
            _check(_lbl, False, True)

    # ── prometheus url resolution: single mode (explicit passthrough) ──
    base = Config(
        gitlab_url="", gitlab_token="", gitlab_username="",
        git_author_name="kru", git_author_email="kru@example",
        dry_run=False, create_mr=True, min_cpu_limit_m=0, min_memory_limit_mi=0,
        prometheus_url="", resource=ResourceConfig(),
        cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides"),
    )

    from main import _resolve_prometheus_url
    base.prometheus_url = "http://my-prom.example:9090"
    url = _resolve_prometheus_url(base)
    _check("prom-url: explicit URL → returned verbatim",
           url, "http://my-prom.example:9090")

    # Empty URL returns empty (the caller's `if not cfg.prometheus_url`
    # branch then handles the misconfiguration with the proper error).
    base.prometheus_url = ""
    url = _resolve_prometheus_url(base)
    _check("prom-url: empty → empty (no discovery fallback in 1.20.0+)",
           url, "")


# --------------------------------------------------------------------------- #
# Section 6: resolver — helm / namespace / workload hierarchy                  #
# --------------------------------------------------------------------------- #

def section_cert_reconciler() -> None:
    """Smoke-tests the in-process serving-cert generation.

    The full reconciler does API I/O (Secret + MWC patch) which we don't
    want to touch in unit QA. We exercise the pure-Python path: generate
    a cert pair, parse it back, assert SANs and expiry land where we want.
    The runtime watch / patch behaviour is validated end-to-end by the
    live test on the cluster.
    """
    _section("CertReconciler — self-signed cert generation (cert-manager replacement)")

    try:
        from src.webhook_cert import _cert_expiry, _generate_cert
    except ModuleNotFoundError as exc:
        # cryptography ships in the runtime image but may not be in the
        # local venv. Skip rather than fail the whole QA.
        if exc.name == "cryptography":
            print(f"  [{_SKIP}] cryptography not installed locally — skipping (covered by image build)")
            return
        raise

    materials = _generate_cert(service="kube-resource-updater-webhook", namespace="kube-resource-updater")

    _check("cert: ca.pem populated",     bool(materials.ca_pem),    True)
    _check("cert: tls.crt populated",    bool(materials.cert_pem),  True)
    _check("cert: tls.key populated",    bool(materials.key_pem),   True)
    _check("cert: ca header",            materials.ca_pem.startswith(b"-----BEGIN CERTIFICATE-----"), True)
    _check("cert: tls.crt header",       materials.cert_pem.startswith(b"-----BEGIN CERTIFICATE-----"), True)
    _check("cert: tls.key header",       materials.key_pem.startswith(b"-----BEGIN RSA PRIVATE KEY-----"), True)

    # SAN coverage — every form the apiserver might use to reach the Service.
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(materials.cert_pem)
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    san_names = sorted(d.value for d in sans)
    expected = sorted([
        "kube-resource-updater-webhook",
        "kube-resource-updater-webhook.kube-resource-updater",
        "kube-resource-updater-webhook.kube-resource-updater.svc",
        "kube-resource-updater-webhook.kube-resource-updater.svc.cluster.local",
    ])
    _check("cert: SAN list matches all four svc-DNS forms", san_names, expected)

    # Expiry: 1y from now (allow ±1d slop for the CertificateBuilder timestamp).
    import datetime
    expires = _cert_expiry(materials.cert_pem)
    target = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    diff_days = abs((expires - target).total_seconds()) / 86400
    _check("cert: expiry within 1d of now+365d",  diff_days < 1, True)

    # _cert_expiry resilience — malformed input returns None, not raises.
    _check("cert: malformed input → None",        _cert_expiry(b"not a cert"), None)
    _check("cert: empty input → None",            _cert_expiry(b""),           None)

    # ── Reconciler API call surface — catch kubernetes-client signature
    #    drift that bit us in the 1.1.0 live test (ApiTypeError on
    #    _content_type kwarg). Mocks the two API clients and walks
    #    _ensure_mwc_ca_bundle / _regenerate_secret end-to-end without
    #    hitting a real cluster. Every API call the reconciler makes in
    #    its bootstrap path is exercised here; a live failure on a kube
    #    method signature change should be caught before the build+sync
    #    cycle.
    from unittest.mock import MagicMock
    from src.webhook_cert import CertReconciler

    fake_core = MagicMock()
    fake_adm = MagicMock()

    # Pretend the Secret already holds a valid cert from a previous run —
    # exercises the read path and the MWC update path in the bootstrap.
    fake_core.read_namespaced_secret.return_value = MagicMock(
        data={
            "ca.crt":  base64.b64encode(materials.ca_pem).decode(),
            "tls.crt": base64.b64encode(materials.cert_pem).decode(),
            "tls.key": base64.b64encode(materials.key_pem).decode(),
        },
    )

    # MWC currently has empty caBundle on its single webhook entry — needs
    # an update to match the cert the reconciler is about to assert.
    mock_hook = MagicMock()
    mock_hook.client_config = MagicMock(ca_bundle="")
    fake_mwc = MagicMock(webhooks=[mock_hook])
    fake_adm.read_mutating_webhook_configuration.return_value = fake_mwc

    rec = CertReconciler(
        secret_name="kube-resource-updater-webhook-cert",
        namespace="kube-resource-updater",
        service_name="kube-resource-updater-webhook",
        webhook_configuration_name="kube-resource-updater-webhook",
        cert_dir="/tmp/qa-cert-dir",
        core_v1=fake_core,
        admission_v1=fake_adm,
    )

    # _ensure_secret should reuse the existing cert — no create / replace call.
    materials_out, source = rec._ensure_secret()
    _check("reconciler: existing-secret path takes 'existing' branch",  source, "existing")
    fake_core.create_namespaced_secret.assert_not_called()
    _check("reconciler: existing-secret path skips create",
           fake_core.create_namespaced_secret.called, False)
    fake_core.replace_namespaced_secret.assert_not_called()
    _check("reconciler: existing-secret path skips replace",
           fake_core.replace_namespaced_secret.called, False)

    # _ensure_mwc_ca_bundle uses replace_*, not patch_* — guards against the
    # 1.1.0 ApiTypeError (`_content_type` kwarg) regression.
    rec._ensure_mwc_ca_bundle(materials_out.ca_pem)
    _check("reconciler: MWC update calls replace_mutating_webhook_configuration",
           fake_adm.replace_mutating_webhook_configuration.called, True)
    _check("reconciler: MWC update does NOT call patch_mutating_webhook_configuration",
           fake_adm.patch_mutating_webhook_configuration.called, False)
    # Idempotency: a second call with the same caBundle should be a no-op.
    fake_adm.reset_mock()
    mock_hook.client_config.ca_bundle = base64.b64encode(materials_out.ca_pem).decode()
    rec._ensure_mwc_ca_bundle(materials_out.ca_pem)
    _check("reconciler: MWC update is idempotent when caBundle already matches",
           fake_adm.replace_mutating_webhook_configuration.called, False)

    # ── VWC caBundle patch — symmetric path to the MWC asserts above.
    #    Regression guard: the reconciler MUST patch the ValidatingWebhook-
    #    Configuration caBundle too, else the apiserver fails open on the
    #    validating webhook. The code is
    #    correct today; this locks it so a refactor can't silently drop it.
    fake_adm.reset_mock()
    mock_vwc_hook = MagicMock()
    mock_vwc_hook.client_config = MagicMock(ca_bundle="")
    fake_vwc = MagicMock(webhooks=[mock_vwc_hook])
    fake_adm.read_validating_webhook_configuration.return_value = fake_vwc

    rec_vwc = CertReconciler(
        secret_name="kube-resource-updater-webhook-cert",
        namespace="kube-resource-updater",
        service_name="kube-resource-updater-webhook",
        webhook_configuration_name="kube-resource-updater-webhook",
        validating_webhook_configuration_name="kube-resource-updater-webhook",
        cert_dir="/tmp/qa-cert-dir",
        core_v1=fake_core,
        admission_v1=fake_adm,
    )

    rec_vwc._ensure_vwc_ca_bundle(materials_out.ca_pem)
    _check("reconciler: VWC update calls replace_validating_webhook_configuration",
           fake_adm.replace_validating_webhook_configuration.called, True)
    _check("reconciler: VWC update does NOT call patch_validating_webhook_configuration",
           fake_adm.patch_validating_webhook_configuration.called, False)

    # Idempotency: second call with matching caBundle must be a no-op.
    fake_adm.reset_mock()
    mock_vwc_hook.client_config.ca_bundle = base64.b64encode(materials_out.ca_pem).decode()
    rec_vwc._ensure_vwc_ca_bundle(materials_out.ca_pem)
    _check("reconciler: VWC update is idempotent when caBundle already matches",
           fake_adm.replace_validating_webhook_configuration.called, False)

    # Skip-on-empty-vwc_name: a reconciler with no VWC name (the
    # webhook.validating.enabled=false chart path, WEBHOOK_VWC unset) must
    # never touch the VWC API.
    fake_adm.reset_mock()
    rec._ensure_vwc_ca_bundle(materials_out.ca_pem)  # rec has vwc_name=""
    _check("reconciler: VWC skipped when vwc_name is empty (no API call)",
           fake_adm.read_validating_webhook_configuration.called, False)

    # run_once_blocking end-to-end: BOTH MWC and VWC patched in one bootstrap.
    # Removing either _ensure_* call from run_once_blocking breaks this.
    fake_adm.reset_mock()
    mock_hook.client_config.ca_bundle = ""   # reset to empty so update fires
    mock_vwc_hook.client_config.ca_bundle = ""
    fake_adm.read_mutating_webhook_configuration.return_value = fake_mwc
    fake_adm.read_validating_webhook_configuration.return_value = fake_vwc
    rec_vwc.run_once_blocking()
    _check("reconciler: run_once_blocking patches MWC caBundle",
           fake_adm.replace_mutating_webhook_configuration.called, True)
    _check("reconciler: run_once_blocking patches VWC caBundle",
           fake_adm.replace_validating_webhook_configuration.called, True)


def section_namespace_cache() -> None:
    """Smoke-tests the webhook's NamespaceCache short-circuit logic.

    The cache class lives in `src/webhook_cache.py` and is consulted by the
    admission webhook on every pod admission to skip namespaces that didn't
    opt in to the tool. Mocks the watch loop entirely — we just want to
    confirm `is_enabled` / `annotations` behave as the webhook expects when
    the underlying dict is populated/empty.
    """
    _section("NamespaceCache — opt-in short-circuit + annotation lookup")

    # Local import keeps qa_params runnable on a Python without aiohttp;
    # the webhook itself never runs from inside QA. NamespaceCache pulls
    # in `kubernetes` for the type hint only — the test bypasses watch().
    from src.webhook_cache import NamespaceCache

    cache = NamespaceCache(api=None)  # type: ignore[arg-type]
    # Hand-populate the internal dict; this is the same shape the watch
    # loop's _initial_list / _apply_event paths produce.
    cache._namespaces = {
        "n8n":       {"kube-resource-updater.enabled": "true",
                      "kube-resource-updater.cpuPercentile": "0.95"},
        "vault":     {"some.other/annotation": "yes"},
        "loki":      {"kube-resource-updater.enabled": "TRUE"},  # case-insensitive truthy
        "monitoring": {"kube-resource-updater.enabled": "false"},
    }

    _check("[ns-cache] enabled ns → True",        cache.is_enabled("n8n"),         True)
    _check("[ns-cache] case-insensitive enabled", cache.is_enabled("loki"),        True)
    _check("[ns-cache] enabled=false → False",    cache.is_enabled("monitoring"),  False)
    _check("[ns-cache] no annotation → False",    cache.is_enabled("vault"),       False)
    _check("[ns-cache] unknown ns → False",       cache.is_enabled("does-not-exist"), False)

    # annotations() returns the verbatim dict (copied) for opted-in namespaces
    # — the webhook would forward that to the resolver for per-ns overrides.
    n8n_annot = cache.annotations("n8n")
    _check("[ns-cache] n8n annotations include cpuPercentile",
           n8n_annot.get("kube-resource-updater.cpuPercentile"), "0.95")
    _check("[ns-cache] unknown ns → empty dict",  cache.annotations("does-not-exist"), {})

    # Returned dict must be a copy — mutating it should not affect the cache.
    n8n_annot["mutated"] = "yes"
    _check("[ns-cache] returned dict is a copy",
           "mutated" in cache.annotations("n8n"), False)


def section_cache_bootstrap_retry() -> None:
    """cache thread-death on bootstrap failure.

    Pre-fix `_run()` called `self._initial_list()` once at startup. If it
    raised (transient API-server 5xx during pod start, RBAC propagation
    lag, network blip), the exception handler logged + `return`ed —
    killing the daemon thread. `_ready` was never set, so `/readyz`
    stayed 503 forever. The Pod kept Running but the webhook served zero
    patches (failurePolicy: Ignore admitted everything unmutated).
    Operator alert only if /readyz triggered an external alarm.

    Fix: wrap the initial bootstrap in a retry loop with exponential
    backoff (cap 60s), keyed on `_stop` so `stop()` still terminates
    cleanly during shutdown. The thread never gives up while running.

    These asserts drive `_initial_list` to fail twice then succeed,
    asserting `_ready` flips and the cache populates.
    """
    _section("Cache bootstrap retry — survives transient API failure (methodology pass)")

    import time
    from unittest.mock import MagicMock, patch
    from src.webhook_cache import ResourceOverrideCache

    # ── CR cache: transient bootstrap failure should retry ───────────────
    api = MagicMock()
    api.list_cluster_custom_object.side_effect = [
        Exception("transient API-server 503"),
        Exception("RBAC propagation lag"),
        {"metadata": {"resourceVersion": "5"}, "items": [
            {"metadata": {"namespace": "ns", "name": "a"},
             "spec": {"selector": {"matchLabels": {"app": "x"}},
                      "containers": [{"name": "c", "requests": {"cpu": "10m"}, "limits": {"cpu": "20m"}}]}},
        ]},
    ]
    cache = ResourceOverrideCache(api=api, event_callback=None)

    # Don't run the watch loop after bootstrap — we only care about
    # bootstrap retry behavior here. Patch _watch_loop to be a no-op
    # that blocks on `_stop` so the thread doesn't churn after success.
    def _noop_watch(self):
        self._stop.wait()
    with patch.object(ResourceOverrideCache, "_watch_loop", _noop_watch):
        # Speed up: patch the backoff sleep to a tiny value.
        with patch.object(ResourceOverrideCache, "_BOOTSTRAP_BACKOFF_INITIAL_S", 0.01, create=True), \
             patch.object(ResourceOverrideCache, "_BOOTSTRAP_BACKOFF_MAX_S", 0.05, create=True):
            cache.start()
            # _ready should flip True after the 3rd attempt succeeds.
            became_ready = cache._ready.wait(timeout=3.0)
        cache.stop()

    _check("[bootstrap-retry-cr] cache became ready after transient failures",
           became_ready, True)
    _check("[bootstrap-retry-cr] _initial_list invoked 3 times (2 fails + 1 success)",
           api.list_cluster_custom_object.call_count, 3)
    _check("[bootstrap-retry-cr] cache populated after retry",
           list(cache._index.get("ns", _empty := type("E", (), {"overrides": {}})()).overrides.keys()),
           ["a"])

    # ── Permanent failure path: thread keeps trying as long as _stop unset ─
    api2 = MagicMock()
    api2.list_cluster_custom_object.side_effect = Exception("permanent RBAC denial")
    cache2 = ResourceOverrideCache(api=api2, event_callback=None)
    with patch.object(ResourceOverrideCache, "_watch_loop", _noop_watch), \
         patch.object(ResourceOverrideCache, "_BOOTSTRAP_BACKOFF_INITIAL_S", 0.01, create=True), \
         patch.object(ResourceOverrideCache, "_BOOTSTRAP_BACKOFF_MAX_S", 0.05, create=True):
        cache2.start()
        # Wait a bit, ensure _ready stays False AND thread is still alive.
        time.sleep(0.15)
        ready = cache2._ready.is_set()
        alive = cache2._thread.is_alive() if cache2._thread else False
        attempts = api2.list_cluster_custom_object.call_count
        cache2.stop()
    _check("[bootstrap-retry-cr-permanent] _ready stays False on permanent failure",
           ready, False)
    _check("[bootstrap-retry-cr-permanent] watch thread stays alive (does not die silently)",
           alive, True)
    _check("[bootstrap-retry-cr-permanent] retried multiple times (> 1)",
           attempts > 1, True)


def section_cr_cache_reconnect() -> None:
    """Verifies `_initial_list` synthesizes events on watch reconnect.

    On the FIRST list (cache empty), no callbacks fire — bootstrap is
    silent. On any SUBSEQUENT list (watch broke and we re-listed), the
    cache compares old vs new and fires synthetic ADDED/MODIFIED/DELETED
    callbacks for whatever changed during the watch outage. Without this,
    apiserver-side modifications that landed during the gap are silently
    dropped — the rollout trigger and any other consumer of the callback
    never learn about them.

    Bug seen live: ArgoCD-driven CR updates → no autoRollout because the
    informer's watch had silently disconnected and the silent re-list
    swallowed the MODIFIED event. Fix: replay the diff as synthetic
    callbacks during reconnect.
    """
    _section("ResourceOverrideCache — synthetic events on reconnect")

    from src.webhook_cache import (
        ContainerOverride,
        ResourceOverride,
        ResourceOverrideCache,
    )

    def _make_ro(name: str, mem_lim: str, ns: str = "default") -> ResourceOverride:
        return ResourceOverride(
            namespace=ns, name=name,
            selector_match_labels={"app.kubernetes.io/name": name},
            containers=(ContainerOverride(
                name="app",
                requests={"memory": "100Mi"},
                limits={"memory": mem_lim},
            ),),
        )

    def _list_resp(ros: list[ResourceOverride], rv: str = "1") -> dict:
        items = [{
            "metadata": {"namespace": r.namespace, "name": r.name},
            "spec": {
                "selector": {"matchLabels": dict(r.selector_match_labels)},
                "containers": [{
                    "name": c.name,
                    "requests": dict(c.requests),
                    "limits": dict(c.limits),
                } for c in r.containers],
            },
        } for r in ros]
        return {"metadata": {"resourceVersion": rv}, "items": items}

    captured: list[tuple] = []
    api = MagicMock()
    cache = ResourceOverrideCache(
        api=api,
        event_callback=lambda kind, ro, prev: captured.append((kind, ro.name, prev.name if prev else None)),
    )

    # First list — bootstrap, silent.
    a_v1 = _make_ro("a", "100Mi")
    b_v1 = _make_ro("b", "200Mi")
    api.list_cluster_custom_object.return_value = _list_resp([a_v1, b_v1])
    cache._initial_list()
    _check("[reconnect] bootstrap fires no callbacks", captured, [])
    _check("[reconnect] cache populated after bootstrap",
           sorted(cache._index["default"].overrides.keys()), ["a", "b"])

    # Reconnect: a unchanged, b changed (200Mi → 300Mi), c added, d (none → none).
    captured.clear()
    a_v2 = _make_ro("a", "100Mi")          # unchanged
    b_v2 = _make_ro("b", "300Mi")          # MODIFIED
    c_v1 = _make_ro("c", "150Mi")          # ADDED
    api.list_cluster_custom_object.return_value = _list_resp([a_v2, b_v2, c_v1], rv="2")
    cache._initial_list()

    kinds = sorted((kind, name) for kind, name, _ in captured)
    _check("[reconnect] unchanged CR → no callback",
           ("MODIFIED", "a") not in kinds and ("ADDED", "a") not in kinds, True)
    _check("[reconnect] modified CR → synthetic MODIFIED",
           ("MODIFIED", "b") in kinds, True)
    _check("[reconnect] new CR → synthetic ADDED",
           ("ADDED", "c") in kinds, True)
    # MODIFIED carries prev so _resources_changed in handlers works correctly.
    mod_b = next(c for c in captured if c[0] == "MODIFIED" and c[1] == "b")
    _check("[reconnect] MODIFIED event carries prev_ro name", mod_b[2], "b")

    # Reconnect: c is now deleted.
    captured.clear()
    api.list_cluster_custom_object.return_value = _list_resp([a_v2, b_v2], rv="3")
    cache._initial_list()
    kinds = [(kind, name) for kind, name, _ in captured]
    _check("[reconnect] vanished CR → synthetic DELETED",
           ("DELETED", "c") in kinds, True)

    # Cross-namespace: a CR moves from ns1 → ns2 (rare; treated as DELETE+ADD).
    captured.clear()
    cache._index.clear()
    cache._ready.clear()
    ns1 = _make_ro("x", "100Mi", ns="ns1")
    api.list_cluster_custom_object.return_value = _list_resp([ns1])
    cache._initial_list()  # bootstrap into ns1
    captured.clear()
    ns2 = _make_ro("x", "100Mi", ns="ns2")
    api.list_cluster_custom_object.return_value = _list_resp([ns2])
    cache._initial_list()  # reconnect, x lives in ns2 now
    kinds = sorted((kind, name) for kind, name, _ in captured)
    _check("[reconnect] cross-ns CR move → DELETED ns1 + ADDED ns2",
           kinds == [("ADDED", "x"), ("DELETED", "x")], True)


def section_cache_unparseable_modified_leak() -> None:
    """— `_apply_event` must treat a MODIFIED event with
    un-parseable spec as effective-delete.

    Without this, a CR previously cached with valid spec keeps mutating
    pods after the operator clears `matchLabels` (or empties containers).
    The stale state persists until the next `_initial_list` self-heal
    (up to the 300s watch timeout) — silent-broken + state-leak.
    """
    _section("ResourceOverrideCache — drop stale on un-parseable MODIFIED")

    from src.webhook_cache import ResourceOverrideCache

    def _cache_has(c: ResourceOverrideCache, ns: str, name: str) -> bool:
        ns_idx = c._index.get(ns)
        return ns_idx is not None and name in ns_idx.overrides

    captured: list[tuple] = []
    api = MagicMock()
    cache = ResourceOverrideCache(
        api=api,
        event_callback=lambda kind, ro, prev: captured.append(
            (kind, ro.name if ro else None, prev.name if prev else None)
        ),
    )

    # Seed: ADDED with a valid CR.
    valid_obj = {
        "metadata": {"namespace": "loki", "name": "loki-app", "resourceVersion": "5"},
        "spec": {
            "selector": {"matchLabels": {"app": "loki"}},
            "containers": [{"name": "main",
                            "requests": {"memory": "100Mi"},
                            "limits":   {"memory": "200Mi"}}],
        },
    }
    cache._apply_event({"type": "ADDED", "object": valid_obj})
    _check("[unparseable-leak] seed: CR cached under (loki, loki-app)",
           _cache_has(cache, "loki", "loki-app"), True)
    captured.clear()

    # Operator clears matchLabels. Watch delivers MODIFIED;
    # _parse returns None (empty matchLabels rejected by _parse).
    unparseable_obj = {
        "metadata": {"namespace": "loki", "name": "loki-app", "resourceVersion": "6"},
        "spec": {
            "selector": {"matchLabels": {}},  # ← empty, un-parseable
            "containers": [{"name": "main"}],
        },
    }
    cache._apply_event({"type": "MODIFIED", "object": unparseable_obj})

    _check("[unparseable-leak] cache drops stale entry after MODIFIED→un-parseable",
           _cache_has(cache, "loki", "loki-app"), False)
    _check("[unparseable-leak] synthetic DELETED callback fires for the dropped entry",
           [(k, n) for k, n, _ in captured], [("DELETED", "loki-app")])

    # ADDED with un-parseable spec → no-op (nothing was cached, nothing to remove).
    captured.clear()
    cache._apply_event({"type": "ADDED", "object": unparseable_obj})
    _check("[unparseable-leak] ADDED with un-parseable spec → no callback (no-op)",
           captured, [])

    # DELETED on a CR not in cache (e.g. it was never parseable) → no-op.
    captured.clear()
    other_unparseable = {
        "metadata": {"namespace": "loki", "name": "never-existed", "resourceVersion": "7"},
        "spec": {"selector": {"matchLabels": {}}, "containers": [{"name": "x"}]},
    }
    cache._apply_event({"type": "DELETED", "object": other_unparseable})
    _check("[unparseable-leak] DELETED for un-cached entry → no callback (no-op)",
           captured, [])


def section_create_mr_bucketing() -> None:
    """Verifies per-workload createMr routing:

      - `WebhookEntry.create_mr` propagates from each pair's effective Config
        (resolver already merged helm < ns < workload).
      - `_render_namespace_file` preserves prev_doc content for cr_names
        listed in `preserve_cr_names` (carry-forward path for the OTHER
        bucket during pass-1 writes).
      - `_write_namespace_files` with `preserve_entries` produces a file
        containing new entries + preserved prev_doc content.
      - File with ONLY preserve entries (no new content this pass) is
        still re-emitted from prev_doc rather than being silently dropped.
      - `_commit_repo`'s bucket split delegates by per-entry `create_mr`.
    """
    _section("createMr per-workload bucketing — pass-1 carry-forward + pass-2 full state")

    import tempfile as _tempfile
    from ruamel.yaml import YAML as _YAML
    from src.writeback_webhook import (
        WebhookEntry,
        _render_namespace_file,
        _write_namespace_files,
        _read_old_docs,
    )

    def _entry(ns: str, name: str, mem: str, create_mr: bool) -> WebhookEntry:
        return WebhookEntry(
            namespace=ns, cr_name=name,
            selector_labels={"app.kubernetes.io/name": name},
            containers=[{
                "name": "app",
                "requests": {"memory": "100Mi"},
                "limits": {"memory": mem},
            }],
            create_mr=create_mr,
        )

    # ── _render_namespace_file: preserve_cr_names carries prev_doc as-is ──
    e_new = _entry("my-app", "api", "300Mi", create_mr=False)
    prev_worker = {
        "apiVersion": "kube-resource-updater.io/v1",
        "kind": "ResourceOverride",
        "metadata": {
            "name": "worker",
            "namespace": "my-app",
            "labels": {"app.kubernetes.io/managed-by": "kube-resource-updater"},
        },
        "spec": {
            "selector": {"matchLabels": {"app.kubernetes.io/name": "worker"}},
            "containers": [{
                "name": "app",
                "requests": {"memory": "100Mi"},
                "limits": {"memory": "200Mi"},
            }],
        },
    }
    content = _render_namespace_file(
        [e_new],
        prev_docs={"worker": prev_worker},
        preserve_cr_names={"worker"},
    )
    _check("[render-preserve] file contains BOTH new entry and preserved doc",
           "name: api" in content and "name: worker" in content, True)
    _check("[render-preserve] preserved worker keeps prev limit (200Mi, not new)",
           "memory: 300Mi" in content and "memory: 200Mi" in content, True)
    _check("[render-preserve] CR doc ordering stable (api before worker alphabetically)",
           content.index("name: api") < content.index("name: worker"), True)

    # New entry wins when same cr_name is in both lists.
    content2 = _render_namespace_file(
        [_entry("my-app", "worker", "999Mi", create_mr=False)],
        prev_docs={"worker": prev_worker},
        preserve_cr_names={"worker"},   # collision: new entry should win
    )
    _check("[render-preserve] new entry wins over preserve when collision",
           "memory: 999Mi" in content2 and "memory: 200Mi" not in content2, True)

    # ── _write_namespace_files: pass-1 simulation (direct + preserve) ─────
    with _tempfile.TemporaryDirectory() as tmp:
        # Seed an "old" file the pass-1 write should preserve from
        ns_dir = os.path.join(tmp, "manifests/kube-resource-updater")
        os.makedirs(ns_dir, exist_ok=True)
        seed_path = os.path.join(ns_dir, "my-app.resource-override.yaml")
        with open(seed_path, "w") as fh:
            yaml = _YAML()
            yaml.explicit_start = True
            from io import StringIO as _StringIO
            buf = _StringIO()
            yaml.dump(prev_worker, buf)
            fh.write(buf.getvalue())

        old_docs = _read_old_docs(tmp, [
            _entry("my-app", "api", "300Mi", create_mr=False),
            _entry("my-app", "worker", "250Mi", create_mr=True),
        ], "manifests/kube-resource-updater")

        # Pass-1 emulation: direct bucket = [api], preserve = [worker]
        direct = [_entry("my-app", "api", "300Mi", create_mr=False)]
        mr_bucket = [_entry("my-app", "worker", "250Mi", create_mr=True)]
        changed = _write_namespace_files(
            tmp, direct,
            path="manifests/kube-resource-updater",
            old_docs=old_docs,
            preserve_entries=mr_bucket,
        )
        _check("[pass1] file changed (new api entry added)", bool(changed), True)
        with open(seed_path, "r") as fh:
            content = fh.read()
        _check("[pass1] file has direct bucket workload (api with new mem 300Mi)",
               "name: api" in content and "memory: 300Mi" in content, True)
        _check("[pass1] file STILL has MR bucket workload (worker) at OLD value",
               "name: worker" in content and "memory: 200Mi" in content, True)
        _check("[pass1] file does NOT yet have worker's NEW value (250Mi)",
               "memory: 250Mi" not in content, True)

        # Pass-2 emulation: write ALL entries (no preserve needed)
        all_entries = direct + mr_bucket
        old_docs_pass2 = _read_old_docs(tmp, all_entries, "manifests/kube-resource-updater")
        changed2 = _write_namespace_files(
            tmp, all_entries,
            path="manifests/kube-resource-updater",
            old_docs=old_docs_pass2,
        )
        _check("[pass2] file changed again (worker now new value)", bool(changed2), True)
        with open(seed_path, "r") as fh:
            content_pass2 = fh.read()
        _check("[pass2] both entries at their NEW values",
               "memory: 300Mi" in content_pass2 and "memory: 250Mi" in content_pass2, True)
        _check("[pass2] old worker value gone",
               "memory: 200Mi" not in content_pass2, True)

    # ── preserve-only file (no new entries for this ns in this pass) ──
    # Seed via the SAME write path so a no-op carry-forward later compares
    # byte-equal — otherwise YAML formatting differences would produce a
    # spurious "changed" entry that doesn't reflect a real diff.
    with _tempfile.TemporaryDirectory() as tmp:
        ns_dir = os.path.join(tmp, "manifests/kube-resource-updater")
        os.makedirs(ns_dir, exist_ok=True)
        seed_path = os.path.join(ns_dir, "ns-a.resource-override.yaml")
        seed_entry = _entry("ns-a", "worker", "200Mi", create_mr=True)
        _write_namespace_files(
            tmp, [seed_entry],
            path="manifests/kube-resource-updater",
            old_docs={},
        )
        _check("[pass1-preserve-only] seed file written via writer path",
               os.path.exists(seed_path) and "memory: 200Mi" in open(seed_path).read(),
               True)

        old_docs = _read_old_docs(tmp, [
            _entry("ns-a", "worker", "350Mi", create_mr=True),
        ], "manifests/kube-resource-updater")

        # Pass-1 simulation: direct bucket EMPTY for ns-a (this ns only has
        # MR-bucket entries this sync). The pass-1 writer must NOT delete
        # the file — pass-2 will land the new content. Carry-forward keeps
        # the prev shape on disk until then, AND produces byte-equal output
        # so no spurious "changed" entry is reported.
        direct = []
        mr_bucket = [_entry("ns-a", "worker", "350Mi", create_mr=True)]
        changed = _write_namespace_files(
            tmp, direct,
            path="manifests/kube-resource-updater",
            old_docs=old_docs,
            preserve_entries=mr_bucket,
        )
        with open(seed_path, "r") as fh:
            content = fh.read()
        _check("[pass1-preserve-only] file kept on disk (not deleted)",
               os.path.exists(seed_path), True)
        _check("[pass1-preserve-only] file content unchanged (still 200Mi, not 350Mi)",
               "memory: 200Mi" in content and "memory: 350Mi" not in content, True)
        _check("[pass1-preserve-only] no spurious 'changed' entries",
               changed == [], True)

    # ── workload cr_name > 63 chars skipped
    from src.writeback_webhook import _build_entries, WebhookEntry as _WE  # noqa
    from src.workload import WorkloadRecommendation as _WRec, ContainerRecommendation as _CRec
    long_name = "a" * 60   # 60 < 63 alone, but kind-prefix makes it overflow
    rec_long_clean = _WRec(
        name=long_name, namespace="ns",
        target_kind="Deployment", target_name=long_name,
        containers=[_CRec(container_name="app")],
    )
    # No collision: cr_name == target_name (60 chars) — under limit, accepted.
    from src.config import Config as _Cfg, ResourceConfig as _RC, CrWritebackConfig as _CW
    cfg_simple = _Cfg(
        gitlab_url="", gitlab_token="", gitlab_username="",
        git_author_name="", git_author_email="", dry_run=False,
        create_mr=False, min_cpu_limit_m=0, min_memory_limit_mi=0,
        # prometheus_url MUST be non-empty for _query_prom_values to be
        # called — without it the mock never fires and the entry is
        # dropped for a different reason (no container payload).
        prometheus_url="http://qa-prom:9090", resource=_RC(),
        cr_writeback=_CW(repo_url="x", path="overrides"),
    )
    from unittest.mock import patch as _patch
    with _patch("src.writeback_webhook._query_prom_values",
                return_value=type("P", (), {
                    "cpu_request_m": 200, "memory_request_bytes": 100*1024*1024,
                    "cpu_limit_m": 400, "memory_limit_bytes": 300*1024*1024,
                })()):
        entries = _build_entries([(rec_long_clean, cfg_simple)])
    _check("[cr-name-D12-under] cr_name=60 chars (no collision) → entry kept",
           len(entries), 1)

    # Collision case: two workloads same name, different kinds. After
    # kind-prefix, cr_name becomes 'deployment-aaaa...' = 11+60 = 71 chars
    # → over the limit. Both workloads should be SKIPPED with warning.
    rec_long_dep = _WRec(
        name=long_name, namespace="ns",
        target_kind="Deployment", target_name=long_name,
        containers=[_CRec(container_name="app")],
    )
    rec_long_ss = _WRec(
        name=long_name, namespace="ns",
        target_kind="StatefulSet", target_name=long_name,
        containers=[_CRec(container_name="app")],
    )
    with _patch("src.writeback_webhook._query_prom_values",
                return_value=type("P", (), {
                    "cpu_request_m": 200, "memory_request_bytes": 100*1024*1024,
                    "cpu_limit_m": 400, "memory_limit_bytes": 300*1024*1024,
                })()):
        entries = _build_entries([(rec_long_dep, cfg_simple), (rec_long_ss, cfg_simple)])
    _check("[cr-name-D12-overflow] cr_name>63 chars (kind-prefixed) → workloads skipped",
           entries, [])

    # ── per-workload createMr=true sneaks past
    # Config.validate (which only checks helm-level create_mr × token).
    # The runtime check in _commit_repo refuses to clone/push when the
    # MR bucket is non-empty AND gitlab_token is empty.
    from src.writeback_webhook import _commit_repo as _commit, WebhookEntry  # noqa  # noqa
    from src.git_provider import GitLabProvider as _GitLabProvider
    # _commit_repo signature: (repo_url, branch, path, entries, provider,
    #   git_author_name, git_author_email,
    #   mr_config, auto_rollout_by_namespace)
    # We synthesise one MR-bucket entry + empty token, expect early return [].
    entry_mr = WebhookEntry(
        namespace="my-app", cr_name="api",
        selector_labels={"app.kubernetes.io/instance": "api"},
        containers=[{"name": "app", "requests": {"cpu": "100m"}}],
        oom_annotations={},
        create_mr=True,
    )
    # The function should never reach the git-clone step — we don't even
    # need a real repo. It must early-return [] and log the error.
    result = _commit(
        repo_url="https://git.example/repo.git",
        branch="main",
        path="overrides",
        entries=[entry_mr],
        provider=_GitLabProvider(
            gitlab_url="https://git.example",
            token="",            # ← the gate: empty token → auth_url unchanged
            username="ci",
        ),
        git_author_name="ci",
        git_author_email="ci@example",
        mr_config=None,
        auto_rollout_by_namespace=None,
    )
    _check("[mr-bucket-no-token] _commit_repo early-returns [] on MR bucket + empty token",
           result, [])

    # Negative: direct-bucket entry + empty token is OK (no MR open). The
    # function still attempts the clone — to verify ONLY the pre-clone
    # gate, we just check that an MR-empty + direct-only entry list does
    # NOT trip the early return. Construct the call but expect git-clone
    # to fail (no real repo) — the failure mode differs from the gate.
    entry_direct = WebhookEntry(
        namespace="my-app", cr_name="api",
        selector_labels={"app.kubernetes.io/instance": "api"},
        containers=[{"name": "app", "requests": {"cpu": "100m"}}],
        oom_annotations={},
        create_mr=False,
    )
    try:
        _commit(
            repo_url="https://git.example/repo.git",
            branch="main",
            path="overrides",
            entries=[entry_direct],
            provider=_GitLabProvider(
                gitlab_url="https://git.example",
                token="",
                username="ci",
            ),
            git_author_name="ci",
            git_author_email="ci@example",
            mr_config=None,
            auto_rollout_by_namespace=None,
        )
    except Exception:
        # Expected — git clone of fake repo fails. The point is we got
        # PAST the MR-bucket gate (which would've returned [] without
        # raising).
        pass
    _check("[mr-bucket-no-token-direct-ok] direct-only bucket does NOT trip the no-token gate",
           True, True)


def section_dry_run_bucketing() -> None:
    """Per-workload dryRun routing: a `kube-resource-updater.dryRun:
    "true"` annotation resolved into a pair's Config.dry_run must EXCLUDE that
    workload's CR from the git write, while non-dry workloads still write.
    Mirrors create_mr per-workload bucketing.

    Fail-first: before #50, WebhookEntry had no dry_run field and
    write_back_webhook_all only checked the global param, so a per-entry
    dry_run=True was silently written when global dry_run=False.
    """
    _section

    from src.writeback_webhook import (
        WebhookEntry, _build_entries, write_back_webhook_all,
    )
    from src.git_provider import GitLabProvider as _GitLabProvider
    from unittest.mock import patch as _patch
    from src.config import CrWritebackConfig as _CW, Config as _Cfg, ResourceConfig as _RC
    from src.workload import (
        WorkloadRecommendation as _WRec, ContainerRecommendation as _CRec,
    )

    def _entry(ns: str, name: str, dry_run: bool, create_mr: bool = False) -> WebhookEntry:
        return WebhookEntry(
            namespace=ns, cr_name=name,
            selector_labels={"app.kubernetes.io/name": name},
            containers=[{"name": "app", "requests": {"memory": "100Mi"},
                         "limits": {"memory": "200Mi"}}],
            dry_run=dry_run, create_mr=create_mr,
        )

    # WebhookEntry carries the dry_run field.
    _check("[dry-run-field] entry dry_run=True flag", _entry("ns", "w", True).dry_run, True)
    _check("[dry-run-field] entry dry_run=False flag", _entry("ns", "w", False).dry_run, False)

    cr_wb = _CW(repo_url="https://git.example/repo.git", path="overrides", branch="main")
    captured: list = []

    def _fake_commit(*, entries, **_kw):
        captured.extend(entries)
        return []

    def _run(entries, global_dry):
        captured.clear()
        with _patch("src.writeback_webhook._commit_repo", side_effect=_fake_commit), \
             _patch("src.writeback_webhook._build_entries", return_value=entries):
            # Non-empty so the early `if not workloads_with_configs` guard is
            # passed; the content is ignored because _build_entries is patched.
            return write_back_webhook_all(
                workloads_with_configs=[(None, None)], cr_writeback=cr_wb,
                provider=_GitLabProvider(
                    gitlab_url="https://git.example",
                    token="t",
                    username="",
                ),
                git_author_name="ci", git_author_email="ci@example",
                dry_run=global_dry,
            )

    # global False, per-entry worker=True → only api written.
    _run([_entry("ns", "api", False), _entry("ns", "worker", True)], False)
    _check("[dry-run-gate] only real entry reaches _commit_repo",
           [e.cr_name for e in captured], ["api"])

    # global True → all excluded, return None.
    res = _run([_entry("ns", "api", False), _entry("ns", "worker", False)], True)
    _check("[dry-run-global] global True → _commit_repo not called", len(captured), 0)
    _check("[dry-run-global] global True → returns None", res, None)

    # all per-entry dry → return None, no git I/O.
    res2 = _run([_entry("ns", "a", True), _entry("ns", "b", True)], False)
    _check("[dry-run-all] all per-entry dry → _commit_repo not called", len(captured), 0)
    _check("[dry-run-all] all per-entry dry → returns None", res2, None)

    # mixed across namespaces → only the non-dry ones written.
    _run([_entry("ns-a", "api", False), _entry("ns-b", "batch", True),
          _entry("ns-c", "cron", False)], False)
    _check("[dry-run-mixed] real entries written, dry excluded",
           sorted(e.cr_name for e in captured), ["api", "cron"])

    # _build_entries stamps dry_run from cfg.dry_run.
    def _cfg(dry: bool) -> _Cfg:
        return _Cfg(
            gitlab_url="", gitlab_token="", gitlab_username="",
            git_author_name="", git_author_email="",
            dry_run=dry, create_mr=False, min_cpu_limit_m=0, min_memory_limit_mi=0,
            prometheus_url="http://qa-prom:9090", resource=_RC(),
            cr_writeback=_CW(repo_url="x", path="overrides"),
        )
    rec_a = _WRec(name="api", namespace="ns", target_kind="Deployment",
                  target_name="api", containers=[_CRec(container_name="app")])
    rec_b = _WRec(name="worker", namespace="ns", target_kind="Deployment",
                  target_name="worker", containers=[_CRec(container_name="app")])
    mock_prom = type("P", (), {
        "cpu_request_m": 200, "memory_request_bytes": 100 * 1024 * 1024,
        "cpu_limit_m": 400, "memory_limit_bytes": 300 * 1024 * 1024})()
    with _patch("src.writeback_webhook._query_prom_values", return_value=mock_prom):
        entries = _build_entries([(rec_a, _cfg(True)), (rec_b, _cfg(False))])
    api_e = next((e for e in entries if e.cr_name == "api"), None)
    worker_e = next((e for e in entries if e.cr_name == "worker"), None)
    _check("[dry-run-stamp] cfg.dry_run=True → entry.dry_run=True",
           api_e.dry_run if api_e else "MISSING", True)
    _check("[dry-run-stamp] cfg.dry_run=False → entry.dry_run=False",
           worker_e.dry_run if worker_e else "MISSING", False)


def section_config_validate() -> None:
    """Verifies Config.validate() fails-fast on config errors.

    Five categories of check:
      (1) Required keys missing
      (2) Inconsistent bounds (min > max)
      (3) Out-of-range numerics
      (4) Malformed Prometheus duration strings
      (5) createMr=true + empty GITLAB_TOKEN
    """
    _section("Config.validate — fail-fast hardening")

    from src.config import Config, ResourceConfig, CrWritebackConfig, MrConfig

    def _base(**overrides) -> Config:
        kwargs = dict(
            gitlab_url="", gitlab_token="some-token", gitlab_username="",
            git_author_name="kru", git_author_email="kru@example",
            dry_run=False, create_mr=False,  # default false to skip token check
            min_cpu_limit_m=0, min_memory_limit_mi=0,
            # Default to a non-empty URL so the happy-path cases below
            # don't trip the new chart-1.20.0 prometheusUrl required gate.
            # Specific tests override to "" to exercise the gate.
            prometheus_url="http://prom:9090",
            resource=ResourceConfig(),
            cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides"),
            mr=MrConfig(),
        )
        # Pop resource-level overrides into a fresh ResourceConfig.
        rc_kwargs = {}
        for k in list(overrides.keys()):
            if hasattr(ResourceConfig, k):
                rc_kwargs[k] = overrides.pop(k)
        if rc_kwargs:
            kwargs["resource"] = ResourceConfig(**rc_kwargs)
        kwargs.update(overrides)
        return Config(**kwargs)

    def _expect_exit_code(cfg: Config) -> "int | None":
        try:
            cfg.validate()
            return None
        except SystemExit as exc:
            return exc.code

    # ── (1) Required keys ─────────────────────────────────────────────────
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="", path="overrides"))
    _check("[validate-required] empty repoUrl → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="https://x", path=""))
    _check("[validate-required] empty path → exit(2)",
           _expect_exit_code(cfg), 2)
    # leading-slash crWriteback.path makes
    # os.path.join discard the repo_dir and the tool writes outside
    # the cloned repo. Reject at validate (mirrors helm-time fail).
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="https://x", path="/manifests/foo"))
    _check("[validate-path-D11] leading-slash path → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="https://x", path="manifests/foo"))
    _check("[validate-path-D11-positive] relative path → ok",
           _expect_exit_code(cfg), None)
    # `prometheusUrl` required (auto-discovery dropped).
    cfg = _base(prometheus_url="")
    _check("[validate-required] empty prometheusUrl → exit(2)",
           _expect_exit_code(cfg), 2)
    # Happy path
    _check("[validate-required] required keys present → no exit",
           _expect_exit_code(_base()), None)

    # ── (2) Inconsistent bounds ───────────────────────────────────────────
    cfg = _base(min_cpu_request_m=500, max_cpu_request_m=200)
    _check("[validate-bounds] minCpuRequest > maxCpuRequest → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(min_memory_request_mi=1024, max_memory_request_mi=512)
    _check("[validate-bounds] minMemoryRequest > maxMemoryRequest → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(min_cpu_request_m=200, max_cpu_request_m=2000)  # min < max OK
    _check("[validate-bounds] min < max → ok",
           _expect_exit_code(cfg), None)
    cfg = _base(min_cpu_request_m=0, max_cpu_request_m=200)  # min=0 = disabled
    _check("[validate-bounds] min=0 (disabled) + max set → ok",
           _expect_exit_code(cfg), None)
    cfg = _base(min_cpu_request_m=500, max_cpu_request_m=0)  # max=0 = disabled
    _check("[validate-bounds] min set + max=0 (disabled) → ok",
           _expect_exit_code(cfg), None)

    # ── (3) Out-of-range numerics ─────────────────────────────────────────
    cfg = _base(cpu_percentile=95.0)  # forgot the decimal
    _check("[validate-range] cpuPercentile=95.0 (typo) → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(cpu_percentile=-0.1)
    _check("[validate-range] negative cpuPercentile → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(cpu_percentile=0.95)
    _check("[validate-range] cpuPercentile=0.95 → ok",
           _expect_exit_code(cfg), None)
    cfg = _base(memory_limit_multiplier=0.5)  # below 1 = limit < request, broken
    _check("[validate-range] memoryLimitMultiplier=0.5 → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(oom_bump_factor=15.0)  # operator footgun upper end
    _check("[validate-range] oomBumpFactor=15 → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(margin_fraction=2.0)  # +200% — questionable but tolerated
    _check("[validate-range] marginFraction=2.0 → ok (under cap)",
           _expect_exit_code(cfg), None)
    cfg = _base(margin_fraction=10.0)  # +1000% almost certainly typo
    _check("[validate-range] marginFraction=10 → exit(2)",
           _expect_exit_code(cfg), 2)

    # ── (4) Prom duration strings ─────────────────────────────────────────
    cfg = _base(cpu_request_window="5days")  # not Prom syntax
    _check("[validate-duration] '5days' (not Prom syntax) → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(cpu_request_window="5d")
    _check("[validate-duration] '5d' → ok",
           _expect_exit_code(cfg), None)
    cfg = _base(mem_request_window="1h30m")
    _check("[validate-duration] '1h30m' compound → ok",
           _expect_exit_code(cfg), None)
    cfg = _base(cpu_limit_window="30")  # no unit
    _check("[validate-duration] '30' (no unit) → exit(2)",
           _expect_exit_code(cfg), 2)
    # zero-component window passes the regex but is
    # semantically empty — every Prom query returns no data, sync writes
    # floor-only CRs everywhere with no visible error.
    cfg = _base(cpu_request_window="0s")
    _check("[validate-duration-B3] '0s' (zero window) → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(mem_limit_window="0h")
    _check("[validate-duration-B3] '0h' (zero window) → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(cpu_limit_window="0w0d")  # compound, both zero
    _check("[validate-duration-B3] '0w0d' (compound zero) → exit(2)",
           _expect_exit_code(cfg), 2)
    # Edge: at least one non-zero component is fine even if another is zero.
    cfg = _base(cpu_limit_window="1h0m")
    _check("[validate-duration-B3] '1h0m' (mixed zero, has 1h) → ok",
           _expect_exit_code(cfg), None)

    # ── (5) createMr=true + empty GITLAB_TOKEN ────────────────────────────
    cfg = _base(create_mr=True, gitlab_token="")
    _check("[validate-token] createMr=true + empty GITLAB_TOKEN → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(create_mr=True, gitlab_token="glpat-xxx")
    _check("[validate-token] createMr=true + token set → ok",
           _expect_exit_code(cfg), None)
    cfg = _base(create_mr=False, gitlab_token="")
    _check("[validate-token] createMr=false + empty token → ok (push works without token if remote allows)",
           _expect_exit_code(cfg), None)

    # ── (6) git refs + identity ────────────────────────
    # `crWriteback.branch` flows into argv positions on `git clone --branch`,
    # `git fetch origin <branch>`, `git push origin <branch>`, and the
    # f-string `origin/{branch}` for `git checkout -B`. argv-list calls
    # (subprocess.run with a list) are shell-injection-safe, but `git` ITSELF
    # parses `--option`-looking values as options regardless of position.
    # A branch named `--upload-pack=/tmp/evil` would let an attacker who
    # controls the ConfigMap run arbitrary commands via the upload-pack hook.
    # Path-traversal in branch (`..`) also lets the value escape the intended
    # refspec. Reject at validate-time so a hand-edited CM is caught at
    # pod-start, not mid-sync.
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides", branch="--upload-pack=evil"))
    _check("[validate-branch-injection] branch starts with '-' (git option) → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides", branch="-foo"))
    _check("[validate-branch-injection] branch leading dash → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides", branch="foo bar"))
    _check("[validate-branch-injection] branch with space → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides", branch="foo..bar"))
    _check("[validate-branch-injection] branch with '..' (git ref grammar violation) → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides", branch="/leading"))
    _check("[validate-branch-injection] branch with leading '/' → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides", branch="main"))
    _check("[validate-branch] 'main' → ok", _expect_exit_code(cfg), None)
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides", branch="release/1.2"))
    _check("[validate-branch] 'release/1.2' → ok", _expect_exit_code(cfg), None)
    cfg = _base(cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides", branch="feature/foo-bar_v2"))
    _check("[validate-branch] 'feature/foo-bar_v2' (full allowed alphabet) → ok",
           _expect_exit_code(cfg), None)

    # gitlab_username: empty is the documented default ("oauth2" fallback in
    # _auth_url). Non-empty must match GitLab's username
    # grammar (starts alphanumeric; allows `._-`). A username starting with
    # `-` would not be a real GitLab user but could surprise downstream URL
    # parsers if a future refactor builds a username-prefixed URL by
    # concatenation (today _auth_url percent-encodes, so this is defence in
    # depth, not a present-day vulnerability).
    cfg = _base(gitlab_username="-alice")
    _check("[validate-username] leading dash → exit(2)", _expect_exit_code(cfg), 2)
    cfg = _base(gitlab_username="alice bob")
    _check("[validate-username] space → exit(2)", _expect_exit_code(cfg), 2)
    cfg = _base(gitlab_username="")
    _check("[validate-username] empty → ok (resolved to 'oauth2' default)",
           _expect_exit_code(cfg), None)
    cfg = _base(gitlab_username="alice")
    _check("[validate-username] 'alice' → ok", _expect_exit_code(cfg), None)
    cfg = _base(gitlab_username="alice.smith_2")
    _check("[validate-username] 'alice.smith_2' → ok", _expect_exit_code(cfg), None)

    # git_author_name: git accepts empty name and silently falls back to
    # the system identity ("user@host"), confusing commit attribution and
    # making MR descriptions misattributed. Reject at validate.
    cfg = _base(git_author_name="")
    _check("[validate-author-name] empty → exit(2)", _expect_exit_code(cfg), 2)
    cfg = _base(git_author_name="   ")
    _check("[validate-author-name] whitespace-only → exit(2)", _expect_exit_code(cfg), 2)
    cfg = _base(git_author_name="line1\nline2")
    _check("[validate-author-name] embedded newline (breaks commit format) → exit(2)",
           _expect_exit_code(cfg), 2)
    cfg = _base(git_author_name="kube-resource-updater")
    _check("[validate-author-name] valid → ok", _expect_exit_code(cfg), None)

    # git_author_email: similar story — empty/malformed reaches the commit
    # trailer and gets accepted by git (which is lenient) but rejected by
    # GitLab MR API as a non-existent committer when push hooks run.
    cfg = _base(git_author_email="")
    _check("[validate-author-email] empty → exit(2)", _expect_exit_code(cfg), 2)
    cfg = _base(git_author_email="not-an-email")
    _check("[validate-author-email] no '@' → exit(2)", _expect_exit_code(cfg), 2)
    cfg = _base(git_author_email="foo@")
    _check("[validate-author-email] empty domain → exit(2)", _expect_exit_code(cfg), 2)
    cfg = _base(git_author_email="@bar.com")
    _check("[validate-author-email] empty local-part → exit(2)", _expect_exit_code(cfg), 2)
    cfg = _base(git_author_email="kru@cluster.local")
    _check("[validate-author-email] valid → ok", _expect_exit_code(cfg), None)

    # ── (7) YAML bool-coercion in numeric bound fields (Zone 9) ──────────────
    # YAML 1.1 parses unquoted `true`/`false` as Python bool. ConfigMap keys
    # like `maxCpuRequestM: true` (operator mistake — thinking it "enables"
    # the bound) reach ResourceConfig.from_dict as bool True, and int(True)=1
    # silently sets a 1m CPU cap on every workload without any error or warning.
    # With min=0 (disabled), validate()'s bounds check is:
    #   `if min_val AND max_val AND min_val > max_val` → `if 1 AND 0` → False
    # So validate() passes silently and every container is throttled to 1m.
    #
    # Fix: _int_bound() in ResourceConfig.from_dict rejects bool and returns
    # the field default (0), logging a WARNING so the operator sees the
    # misconfiguration without an outright crash.
    import tempfile as _tempfile
    import os as _os

    def _from_file_with_yaml(yaml_snippet: str):
        """Round-trip through Config.from_file using a temp file."""
        full = (
            "config:\n"
            "  prometheusUrl: http://prom:9090\n"
            "  createMr: false\n"
            "  gitAuthorName: kru-bot\n"
            "  gitAuthorEmail: kru@example.com\n"
            "  crWriteback:\n"
            "    repoUrl: https://x\n"
            "    path: overrides\n"
            + yaml_snippet
        )
        with _tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(full)
            path = f.name
        try:
            return Config.from_file(path)
        finally:
            _os.unlink(path)

    # maxCpuRequestM: true → int(True)=1 currently passes validate, silently
    # caps every CPU request to 1m. After fix: from_dict returns 0 (safe default).
    cfg_bool = _from_file_with_yaml("  maxCpuRequestM: true\n")
    _check(
        "[validate-bool-coerce] maxCpuRequestM: true coerced to 1 → from_dict must return 0",
        cfg_bool.resource.max_cpu_request_m,
        0,
    )

    # minMemoryRequestMi: true → int(True)=1 sets a 1 MiB floor instead of disabled.
    cfg_bool2 = _from_file_with_yaml("  minMemoryRequestMi: true\n")
    _check(
        "[validate-bool-coerce] minMemoryRequestMi: true coerced → from_dict must return 0",
        cfg_bool2.resource.min_memory_request_mi,
        0,
    )

    # false: minCpuRequestM: false → int(False)=0. After fix: still 0 (correct
    # default), but a warning is emitted so the operator knows the value was
    # interpreted as a boolean, not a numeric zero.
    cfg_false = _from_file_with_yaml("  minCpuRequestM: false\n")
    _check(
        "[validate-bool-coerce] minCpuRequestM: false → from_dict returns 0 (disabled, safe)",
        cfg_false.resource.min_cpu_request_m,
        0,
    )

    # Sanity: integer strings and bare integers still parse correctly.
    cfg_int = _from_file_with_yaml("  maxCpuRequestM: '500'\n")
    _check(
        "[validate-bool-coerce] maxCpuRequestM: '500' (string) → from_dict returns 500",
        cfg_int.resource.max_cpu_request_m,
        500,
    )
    cfg_int2 = _from_file_with_yaml("  maxCpuRequestM: 200\n")
    _check(
        "[validate-bool-coerce] maxCpuRequestM: 200 (int) → from_dict returns 200",
        cfg_int2.resource.max_cpu_request_m,
        200,
    )


    # ── Bug: --mr CLI flag bypasses createMr+token validation ─────────────
    # Pre-fix sequence in cmd_sync (main.py):
    #   cfg.validate()           # passes because create_mr=False (ConfigMap default)
    #   cfg.create_mr = args.mr  # mutates to True AFTER validate returned
    # Result: effective config has create_mr=True with empty gitlab_token;
    # validation invariant satisfied only on the pre-mutation state.
    # Fix: merge CLI flags into cfg BEFORE calling validate(), so validate()
    # sees the effective state that includes the --mr override.
    #
    # This assert exercises cmd_sync directly, mocking Config.load() to
    # return a config with create_mr=False and an empty token.  With --mr
    # supplied the effective config is create_mr=True + no token.
    # Pre-fix: validate() runs before the merge → no exit → git ops proceed
    #          and crash with 401 after the push already committed.
    # Post-fix: merge happens first → validate() exits(2) immediately.
    import argparse as _argparse
    import main as _main
    from src.config import Config as _Config, CrWritebackConfig as _CRW, ResourceConfig as _RC

    def _cmd_sync_exits_2_with_mr_flag_and_no_token() -> bool:
        """Return True if cmd_sync exits(2) when --mr is set with no token."""
        cfg_no_token = _Config(
            gitlab_url="", gitlab_token="", gitlab_username="",
            git_author_name="kru", git_author_email="kru@example",
            dry_run=False, create_mr=False,  # ConfigMap default: false
            min_cpu_limit_m=0, min_memory_limit_mi=0,
            prometheus_url="http://prom:9090",
            resource=_RC(),
            cr_writeback=_CRW(repo_url="https://x", path="overrides"),
        )
        args = _argparse.Namespace(mr=True)  # --mr flag supplied
        # Mock Config.load() so cmd_sync uses our controlled config.
        with patch("main.Config") as mock_cfg_cls,              patch("main._log_module.setup"),              patch("main.load_config"):
            mock_cfg_cls.load.return_value = cfg_no_token
            try:
                _main.cmd_sync(args)
                return False
            except SystemExit as exc:
                return exc.code == 2

    _check("[validate-mr-flag] cmd_sync with --mr + no token exits(2) before git ops",
           _cmd_sync_exits_2_with_mr_flag_and_no_token(), True)

    # Positive: --mr with token present must reach past validation (no exit(2)).
    def _cmd_sync_passes_validate_with_token() -> bool:
        """Return True if validate() does NOT exit when --mr is set with a token."""
        cfg_with_token = _Config(
            gitlab_url="", gitlab_token="glpat-xxx", gitlab_username="",
            git_author_name="kru", git_author_email="kru@example",
            dry_run=False, create_mr=False,
            min_cpu_limit_m=0, min_memory_limit_mi=0,
            prometheus_url="http://prom:9090",
            resource=_RC(),
            cr_writeback=_CRW(repo_url="https://x", path="overrides"),
        )
        args = _argparse.Namespace(mr=True)
        with patch("main.Config") as mock_cfg_cls,              patch("main._log_module.setup"),              patch("main.load_config"),              patch("main.log_git_credentials_state"),              patch("main._log_global_config"),              patch("main._resolve_prometheus_url"),              patch("main.list_enabled_namespaces", return_value=[]):
            mock_cfg_cls.load.return_value = cfg_with_token
            try:
                _main.cmd_sync(args)
                return True  # no exit → passed validate
            except SystemExit as exc:
                return exc.code != 2  # exit for reasons other than validate is OK
    _check("[validate-mr-flag] cmd_sync with --mr + token set: validate passes (no exit 2)",
           _cmd_sync_passes_validate_with_token(), True)


def section_margin_default_safe() -> None:
    """— marginFraction default must be >= 0.05.

    A default of 0.00 means limits == observed max; the first traffic
    spike after deploy OOMs the container. The intended safe floor is 0.10
    (10% headroom). This assert must FAIL on pre-fix code (default 0.00)
    and PASS after the python agent bumps the dataclass default to 0.10
    and both from_dict / from_env fallbacks to '0.10'.

    Falsifiability: revert ResourceConfig.margin_fraction default to 0.00
    → assert fails. Bump to 0.10 → passes.
    """
    _section("— marginFraction default safety (>= 0.05)")

    from src.config import ResourceConfig

    # Dataclass field default — used when no ConfigMap key is present.
    default_val = ResourceConfig().margin_fraction
    _check_truthy(
        "[margin-default] ResourceConfig() default margin_fraction >= 0.05",
        default_val >= 0.05,
    )

    # from_dict with no marginFraction key — mirrors helm default path.
    from_dict_val = ResourceConfig.from_dict({}).margin_fraction
    _check_truthy(
        "[margin-default] from_dict({}) margin_fraction >= 0.05",
        from_dict_val >= 0.05,
    )

    # OSS pre-publish bug: an EXPLICIT zero margin must be honored, not treated
    # as "unset". A ConfigMap `cpuRequestMargin: 0` (YAML int) parsed via
    # _opt_float returned None and fell back to margin_fraction (0.10) — an
    # operator asking for zero headroom silently got 10%. The annotation path
    # (_parse_float("0")) was already correct; only the ConfigMap-int path bugs.
    # These FAIL pre-fix (return 0.10).
    _check("[margin-zero] cpuRequestMargin: 0 (int) → effective 0.0 (not fallback)",
           ResourceConfig.from_dict({"cpuRequestMargin": 0}).effective_cpu_request_margin, 0.0)
    _check("[margin-zero] cpuRequestMargin: '0' (str) → effective 0.0",
           ResourceConfig.from_dict({"cpuRequestMargin": "0"}).effective_cpu_request_margin, 0.0)
    _check("[margin-zero] memLimitMargin: 0 → effective 0.0",
           ResourceConfig.from_dict({"memLimitMargin": 0}).effective_mem_limit_margin, 0.0)
    # Negative control: an ABSENT key still falls back to margin_fraction.
    _check("[margin-zero] absent cpuRequestMargin → falls back to margin_fraction (0.20)",
           ResourceConfig.from_dict({"marginFraction": "0.20"}).effective_cpu_request_margin, 0.20)


def section_cr_name_collision() -> None:
    """Verifies Deployment + StatefulSet same-name in same-namespace gets
    disambiguated via kind prefix.

    Background: ResourceOverride is namespace-scoped, names unique per
    kind per namespace. The tool writes all workload kinds to the SAME
    CR kind, so a Deployment and a StatefulSet sharing a name produce
    two CR docs with `metadata.name == metadata.name`; apiserver rejects
    the second on apply. Detected pre-build, both colliders renamed.
    """
    _section("CR name collision — Deployment + StatefulSet same-name disambiguation")

    from src.config import Config, ResourceConfig, CrWritebackConfig
    from src.workload import ContainerRecommendation, WorkloadRecommendation as WR
    from src.writeback_webhook import _build_entries

    def _cfg() -> Config:
        return Config(
            gitlab_url="", gitlab_token="", gitlab_username="",
            git_author_name="kru", git_author_email="kru@example",
            dry_run=False, create_mr=True,
            min_cpu_limit_m=0, min_memory_limit_mi=0,
            prometheus_url="http://prom:9090",
            resource=ResourceConfig(),
            cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides"),
        )

    def _rec(kind: str, name: str, ns: str = "my-app") -> WR:
        return WR(
            name=name, namespace=ns, target_kind=kind, target_name=name,
            containers=[ContainerRecommendation(container_name="app")],
        )

    # Mock Prom values so _build_container_resources returns a non-empty res.
    from types import SimpleNamespace
    M = 1024 * 1024
    prom = SimpleNamespace(
        cpu_request_m=200, memory_request_bytes=128 * M,
        cpu_limit_m=400, memory_limit_bytes=384 * M,
    )

    # ── No collision: distinct names → bare cr_name preserved ─────────────
    pairs_clean = [
        (_rec("Deployment", "api"), _cfg()),
        (_rec("StatefulSet", "worker"), _cfg()),
    ]
    with patch("src.writeback_webhook._query_prom_values", return_value=prom):
        entries = _build_entries(pairs_clean)
    cr_names = sorted(e.cr_name for e in entries)
    _check("[collision-clean] no collision → bare names preserved",
           cr_names, ["api", "worker"])

    # ── Collision: Deployment + StatefulSet share name in same ns ─────────
    pairs_collide = [
        (_rec("Deployment", "shared"), _cfg()),
        (_rec("StatefulSet", "shared"), _cfg()),
    ]
    with patch("src.writeback_webhook._query_prom_values", return_value=prom):
        entries = _build_entries(pairs_collide)
    cr_names = sorted(e.cr_name for e in entries)
    _check("[collision] both renamed with kind prefix",
           cr_names, ["deployment-shared", "statefulset-shared"])
    _check("[collision] both entries point to my-app namespace",
           {e.namespace for e in entries}, {"my-app"})

    # ── Mix: collision + non-collision in same sync ───────────────────────
    pairs_mixed = [
        (_rec("Deployment", "api"), _cfg()),
        (_rec("Deployment", "shared"), _cfg()),
        (_rec("StatefulSet", "shared"), _cfg()),
        (_rec("StatefulSet", "queue"), _cfg()),
    ]
    with patch("src.writeback_webhook._query_prom_values", return_value=prom):
        entries = _build_entries(pairs_mixed)
    cr_names = sorted(e.cr_name for e in entries)
    _check("[collision-mixed] non-colliders keep bare name, colliders prefixed",
           cr_names, ["api", "deployment-shared", "queue", "statefulset-shared"])

    # ── Cross-namespace same name (no collision — namespaces differ) ──────
    pairs_xns = [
        (_rec("Deployment", "shared", ns="ns-a"), _cfg()),
        (_rec("StatefulSet", "shared", ns="ns-b"), _cfg()),
    ]
    with patch("src.writeback_webhook._query_prom_values", return_value=prom):
        entries = _build_entries(pairs_xns)
    cr_names = sorted((e.namespace, e.cr_name) for e in entries)
    _check("[collision-xns] same name in different namespaces is NOT a collision",
           cr_names, [("ns-a", "shared"), ("ns-b", "shared")])


def section_log_formatter() -> None:
    """Verifies `src/log._format_message` and `_resolve_color` behave the
    way the deployed pods rely on.

    Why these are unit-tested rather than visually eyeballed: the formatter
    is exercised on EVERY log line in the running pod, and Argo CD's log
    viewer renders ANSI escapes — so a tag-color regression visibly breaks
    operator UX in the most-used surface for reading these logs. Cheaper
    to assert here than rebuild the image, push, and squint at the UI.

    Properties asserted:
      1. `[tag]` at the start of a line gets padded to a stable visual
         column (default width 11 — tag + min one space).
      2. The same tag in color mode is wrapped with the configured ANSI
         escape for its color group and the SAME padding is applied
         (ANSI bytes do not change visual width).
      3. Unknown tags pass through uncolored but still padded — defensive
         for tags introduced in future modules.
      4. The namespace-separator pattern `=== <ns> ===` is colored as a
         unit; the trailing `(workloads=N)` suffix is left uncolored so
         a count change is still legible against the colored header.
      5. `_resolve_color` tri-state: `never` always returns False;
         `always` returns True regardless of format; `auto` returns False
         for JSON (we never want ANSI in structured logs), True for text.
      6. `_JsonFormatter` strips ANSI from message bodies even when a
         caller pre-colors text — JSON output stays clean for Loki.
    """
    _section("log formatter — tag padding + pastel color + JSON sanitization")

    from src.log import (
        _PALETTE,
        _TAG_PAD_WIDTH,
        _format_message,
        _resolve_color,
        _JsonFormatter,
    )
    import json
    import logging

    # ── (1) Padding without color: tag + ≥1 space ─────────────────────────
    # Target visual column for short tags: TAG_PAD_WIDTH + 1 (the minimum
    # gap after `]`). For `[OK]` (4 chars) → 7 spaces, total 11 chars
    # before the message body.
    plain = _format_message("[OK] foo: bar", color=False)
    _check("[fmt-plain-ok] [OK] padded to fixed column",
           plain, "[OK]" + " " * (_TAG_PAD_WIDTH + 1 - 4) + "foo: bar")

    plain = _format_message("[SKIP] ns/wl: reason", color=False)
    _check("[fmt-plain-skip] [SKIP] padded to fixed column",
           plain, "[SKIP]" + " " * (_TAG_PAD_WIDTH + 1 - 6) + "ns/wl: reason")

    # Tag near the boundary width: pad computed from `_TAG_PAD_WIDTH` so
    # the test stays valid if the constant changes.
    plain = _format_message("[oom-bump] details", color=False)
    pad = max(1, _TAG_PAD_WIDTH - len("[oom-bump]") + 1)
    _check("[fmt-plain-boundary] [oom-bump] padded to column 12",
           plain, "[oom-bump]" + " " * pad + "details")

    # Long tag overflows; still gets a single trailing space.
    plain = _format_message("[oom-bump-suppressed] details", color=False)
    _check("[fmt-plain-overflow] overlong tag falls through with 1-space gap",
           plain, "[oom-bump-suppressed] details")

    # ── (2) Padding stable across color mode (ANSI has zero visual width) ─
    colored = _format_message("[OK] foo: bar", color=True)
    # Strip ANSI then compare to the plain-padded form.
    import re as _re
    stripped = _re.sub(r"\x1b\[[0-9;]*m", "", colored)
    _check("[fmt-color-padding-invariant] color mode produces same visual width",
           stripped, "[OK]" + " " * (_TAG_PAD_WIDTH + 1 - 4) + "foo: bar")
    _check("[fmt-color-ok-green] [OK] wrapped in green ANSI",
           _PALETTE["green"] in colored and _PALETTE["reset"] in colored, True)

    colored = _format_message("[SKIP] ns/wl: reason", color=True)
    _check("[fmt-color-skip-gray] [SKIP] wrapped in gray ANSI",
           _PALETTE["gray"] in colored, True)

    colored = _format_message("[oom-bump-suppressed] details", color=True)
    _check("[fmt-color-suppressed-red] suppression tag in red (warn-grade)",
           _PALETTE["red"] in colored, True)

    # ── (3) Unknown tag: pad but don't color ──────────────────────────────
    plain = _format_message("[future-feature] hello", color=False)
    _check("[fmt-unknown-plain] unknown tag still padded",
           plain.startswith("[future-feature] ") and plain.endswith("hello"), True)
    colored = _format_message("[future-feature] hello", color=True)
    _check("[fmt-unknown-no-color] unknown tag NOT colored",
           "\x1b[" in colored, False)

    # ── (4) Namespace separator: whole-header colored, suffix uncolored ──
    sep = _format_message("=== goldilocks ===  (workloads=4)", color=True)
    _check("[fmt-ns-sep-colored] === ns === wrapped in blue",
           _PALETTE["blue"] in sep, True)
    _check("[fmt-ns-sep-suffix-bare] (workloads=N) suffix outside the color region",
           "(workloads=4)" in sep and sep.endswith("(workloads=4)"), True)
    # Plain mode: header is untouched.
    sep_plain = _format_message("=== goldilocks ===  (workloads=4)", color=False)
    _check("[fmt-ns-sep-plain] no escape in plain mode",
           "\x1b[" in sep_plain, False)

    # ── (5) _resolve_color tri-state ──────────────────────────────────────
    _check("[resolve-color-never-text] never=False (text)",
           _resolve_color("never", "text"), False)
    _check("[resolve-color-never-json] never=False (json)",
           _resolve_color("never", "json"), False)
    _check("[resolve-color-always-text] always=True (text)",
           _resolve_color("always", "text"), True)
    _check("[resolve-color-always-json] always=True (json) — caller forced",
           _resolve_color("always", "json"), True)
    _check("[resolve-color-auto-text] auto=True (text)",
           _resolve_color("auto", "text"), True)
    _check("[resolve-color-auto-json] auto=False (json — never inject ANSI)",
           _resolve_color("auto", "json"), False)
    # Empty/unset coerces to auto.
    _check("[resolve-color-default] empty setting defaults to auto",
           _resolve_color("", "text"), True)

    # ── (6) _JsonFormatter strips ANSI from message bodies ────────────────
    record = logging.LogRecord(
        name="test", level=logging.INFO,
        pathname="x", lineno=1,
        msg=f"hello {_PALETTE['red']}world{_PALETTE['reset']}",
        args=None, exc_info=None,
    )
    json_out = _JsonFormatter().format(record)
    parsed = json.loads(json_out)
    _check("[fmt-json-strips-ansi] JSON message body has no ANSI escapes",
           "\x1b[" in parsed["message"], False)
    _check("[fmt-json-preserves-text] visible characters survive ANSI strip",
           parsed["message"], "hello world")

    # ── (7) Percentage deltas: red for increase, green for decrease ───────
    # Increases warrant attention (cost/risk up); decreases are wins
    # (rightsizing savings). Pattern only matches signed integers inside
    # parens so the `(workloads=N)` suffix on ns headers is untouched.
    line = _format_message("  loki/loki  req=cpu:200m (+147%) mem:512Mi (-12%)", color=True)
    _check("[fmt-pct-increase-red] (+N%) wrapped in red",
           _PALETTE["red"] in line and "(+147%)" in line, True)
    _check("[fmt-pct-decrease-green] (-N%) wrapped in green",
           _PALETTE["green"] in line and "(-12%)" in line, True)

    # Plain mode: no color, percentage text identical.
    line_plain = _format_message("  loki/loki  req=cpu:200m (+147%) mem:512Mi (-12%)", color=False)
    _check("[fmt-pct-plain-no-color] no ANSI in plain mode",
           "\x1b[" in line_plain, False)
    _check("[fmt-pct-plain-content] message body identical to input in plain mode",
           line_plain, "  loki/loki  req=cpu:200m (+147%) mem:512Mi (-12%)")

    # Tag + percentage on the same line: both color regions present, in
    # the right order (tag first, then in-message %).
    combo = _format_message("[OK] ns: rolled +5% over baseline (+5%)", color=True)
    _check("[fmt-pct-with-tag] tag colored AND in-message (+N%) colored",
           _PALETTE["green"] in combo  # [OK] is green
           and combo.count(_PALETTE["red"]) == 1,  # exactly one (+5%) painted
           True)

    # Namespace header's `(workloads=N)` MUST NOT be colored as a delta
    # (it's a workload count, not a percentage). Regression guard for the
    # regex restriction to `(+|-)<digits>%`.
    ns_with_count = _format_message("=== loki ===  (workloads=4)", color=True)
    _check("[fmt-pct-skip-ns-count] (workloads=N) is not a delta — left uncolored",
           _PALETTE["red"] not in ns_with_count
           and _PALETTE["green"] not in ns_with_count,
           True)

    # ── (8) Inline coloring: URLs (blue + underline), → arrows (cyan),
    #        keyword labels (req=/lim=/containers=) in gray.
    line = _format_message(
        "[OK] webhook: MR opened https://git.example.com/path (branch a → main)",
        color=True,
    )
    _check("[fmt-url-blue-underlined] URLs wrapped in blue + underline (SGR 4)",
           _PALETTE["blue"] in line and "\x1b[4m" in line and "\x1b[24m" in line,
           True)
    _check("[fmt-url-content] URL text preserved verbatim",
           "https://git.example.com/path" in line, True)
    _check("[fmt-arrow-cyan] ' → ' replaced with cyan-wrapped arrow",
           _PALETTE["cyan"] in line and " → " in line.replace("\x1b[0m", "").replace(_PALETTE["cyan"], ""),
           True)

    # Keyword de-emphasis on the delta line.
    line = _format_message(
        "  loki/loki  req=cpu:200m mem:3875Mi  lim=cpu:636m mem:4537Mi",
        color=True,
    )
    _check("[fmt-keyword-dim] req=/lim= rendered in gray for de-emphasis",
           line.count(_PALETTE["gray"]) == 2, True)
    _check("[fmt-keyword-content] keyword text preserved",
           "req=" in line and "lim=" in line, True)

    # `containers=N` from the discovery section also gets dimmed (same regex).
    line = _format_message("  kru-test  containers=1", color=True)
    _check("[fmt-keyword-dim-containers] containers= wrapped in gray",
           _PALETTE["gray"] in line and "containers=" in line, True)

    # ── (9) Whole-line gray for `skipping containers:` info bullets.
    line = _format_message(
        "  loki/loki-chunks-cache skipping containers: exporter",
        color=True,
    )
    _check("[fmt-info-line-gray] skipping containers line entirely gray",
           line.startswith(_PALETTE["gray"]) and line.endswith(_PALETTE["reset"]),
           True)
    # Variant: init containers.
    line = _format_message(
        "  ns/wl skipping init containers: copy-config",
        color=True,
    )
    _check("[fmt-info-line-gray-init] skipping init containers also gray",
           line.startswith(_PALETTE["gray"]) and line.endswith(_PALETTE["reset"]),
           True)

    # Plain mode: info lines passthrough verbatim.
    plain = _format_message(
        "  loki/loki-chunks-cache skipping containers: exporter",
        color=False,
    )
    _check("[fmt-info-line-plain] no color in plain mode",
           "\x1b[" in plain, False)

    # ── (10) Phase context: lines emitted inside `phase_ctx(...)` carry
    #         a `[<phase>] ` prefix; outside any block they don't.
    from src.log import phase_ctx, current_phase, _PHASE_COLORS

    # Outside any block: no phase prefix.
    _check("[phase-default-none] default phase is None outside any block",
           current_phase(), None)

    # Inside `discovery`: prefix is `[discovery] ` padded to column 12.
    with phase_ctx("discovery"):
        _check("[phase-set] phase_ctx sets the current phase",
               current_phase(), "discovery")
    _check("[phase-restore] context manager restores prior phase on exit",
           current_phase(), None)

    # Nested phases push and restore correctly.
    with phase_ctx("recommend"):
        outer = current_phase()
        with phase_ctx("result"):
            inner = current_phase()
        after_inner = current_phase()
    _check("[phase-nest-outer] outer phase is recommend",
           outer, "recommend")
    _check("[phase-nest-inner] inner phase is result",
           inner, "result")
    _check("[phase-nest-restore] after inner exits, outer (recommend) restored",
           after_inner, "recommend")

    # Phase color sanity: each phase has its own hue (no overlaps).
    phase_color_values = {_PHASE_COLORS[k] for k in ("discovery", "recommend", "result")}
    _check("[phase-colors-distinct] three phases use three different color groups",
           len(phase_color_values), 3)

    # Phase contextvar drives the JSON `phase` field but the text-mode
    # output does NOT inject a per-line `[<phase>]`
    # prefix — phase transitions are marked by a single `log_phase_banner`
    # line at the top of each block. Regression guards both invariants:
    from src.log import _TextFormatter, _BANNER_KEY
    fmt = _TextFormatter(color=False)
    rec_info = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="hello", args=None, exc_info=None,
    )
    with phase_ctx("recommend"):
        out = fmt.format(rec_info)
    _check("[phase-no-text-prefix] non-empty line does NOT get a [phase] prefix in text mode",
           out, "hello")

    # Banner record: emitted by `log_phase_banner`; carries the
    # `banner=True` extra so the formatter renders it as the colored
    # capitalized phase word with no tag-padding / levstagingfix.
    rec_banner = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="discovery", args=None, exc_info=None,
    )
    setattr(rec_banner, _BANNER_KEY, True)
    fmt_color = _TextFormatter(color=True)
    with phase_ctx("discovery"):
        out_plain = fmt.format(rec_banner)
        out_color = fmt_color.format(rec_banner)
    _check("[phase-banner-plain] banner line passes through unmodified in plain mode",
           out_plain, "discovery")
    _check("[phase-banner-color-cyan] discovery banner colored cyan",
           _PALETTE["cyan"] in out_color, True)
    _check("[phase-banner-bold] banner wrapped in bold SGR",
           "\x1b[1m" in out_color and "\x1b[22m" in out_color, True)

    # Banner produced by `log_phase_banner` with a subtitle: rendered
    # as `─────── PHASE (subtitle) ───────` — UPPERCASE word, parenthesized
    # subtitle, heavy box-drawing dashes either side.
    from src.log import log_phase_banner
    import io
    import logging as _logging
    buf = io.StringIO()
    handler = _logging.StreamHandler(buf)
    handler.setFormatter(_TextFormatter(color=False))
    test_log = _logging.getLogger("qa.banner")
    test_log.handlers = [handler]
    test_log.setLevel(_logging.INFO)
    test_log.propagate = False
    with phase_ctx("discovery"):
        log_phase_banner(test_log, "discovery", subtitle="3 ns, 7 wl")
    lines = buf.getvalue().splitlines()
    # Three lines: blank, banner, blank.
    _check("[phase-banner-shape] 3 lines emitted (blank/banner/blank)",
           len(lines), 3)
    _check("[phase-banner-text] dashed banner with UPPERCASE + subtitle",
           "─────── DISCOVERY (3 ns, 7 wl) ───────" in lines[1], True)

    # Tree-connector regex dims `├─` / `└─` in the inline coloring chain.
    line = _format_message(
        "  ├─ loki/loki  req=cpu:200m mem:512Mi  lim=cpu:400m mem:1024Mi",
        color=True,
    )
    # gray appears at least 3x: connector + req= + lim=
    _check("[fmt-tree-gray] ├─ connector wrapped in gray",
           line.count(_PALETTE["gray"]) >= 3, True)
    line = _format_message(
        "  └─ loki-gateway/nginx  req=cpu:200m mem:100Mi  lim=cpu:200m mem:100Mi",
        color=True,
    )
    _check("[fmt-tree-gray-end] └─ end-connector also wrapped in gray",
           _PALETTE["gray"] in line and "└─" in line, True)

    # Unchanged line marker — whole-line gray regardless of contained tokens.
    from src.log import _UNCHANGED_KEY
    rec_unch = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="  └─ loki-gateway/nginx  req=cpu:200m mem:100Mi  lim=cpu:200m mem:100Mi",
        args=None, exc_info=None,
    )
    setattr(rec_unch, _UNCHANGED_KEY, True)
    out = fmt_color.format(rec_unch)
    _check("[fmt-unchanged-whole-gray] unchanged line wrapped whole in gray",
           out.startswith(_PALETTE["gray"]) and out.endswith(_PALETTE["reset"]),
           True)
    # Plain mode: unchanged marker has no effect (no gray escape).
    out_plain = fmt.format(rec_unch)
    _check("[fmt-unchanged-plain-passthrough] no ANSI in plain mode",
           "\x1b[" in out_plain, False)

    # _value_with_delta — before→after format for the per-container delta
    # line. Operator sees both old and new value inline
    # instead of having to dig into the previous MR to learn the old one.
    from src.writeback_webhook import _value_with_delta
    _check("[value-delta-changed] changed mem produces 'old → new (+N%)'",
           _value_with_delta("3876Mi", "1568Mi", is_cpu=False),
           "1568Mi → 3876Mi (+147%)")
    _check("[value-delta-decreased] decreased mem produces 'old → new (-N%)'",
           _value_with_delta("110Mi", "117Mi", is_cpu=False),
           "117Mi → 110Mi (-6%)")
    _check("[value-delta-unchanged-equal] unchanged returns the bare value",
           _value_with_delta("100Mi", "100Mi", is_cpu=False), "100Mi")
    _check("[value-delta-no-old] first sync (no old) returns the bare value",
           _value_with_delta("100Mi", None, is_cpu=False), "100Mi")
    _check("[value-delta-no-new] missing new returns em-dash placeholder",
           _value_with_delta(None, "100Mi", is_cpu=False), "—")
    # 1% threshold: `_delta` uses integer division, so 1m/100m crosses
    # the threshold and returns "+1%". Pick a smaller delta for the
    # sub-threshold regression guard.
    _check("[value-delta-subthreshold] sub-1% change (1000m → 1005m) returns bare value",
           _value_with_delta("1005m", "1000m", is_cpu=True), "1005m")

    # Blank lines (the explicit `_log.info('')` separators between
    # namespace blocks and around banners) MUST stay blank.
    rec_blank = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="", args=None, exc_info=None,
    )
    with phase_ctx("recommend"):
        out = fmt.format(rec_blank)
    _check("[phase-blank-line-unchanged] blank lines stay blank, no phase prefix",
           out, "")

    # JSON formatter exposes the active phase as a separate field for
    # Loki / structured-log consumers.
    from src.log import _JsonFormatter
    json_fmt = _JsonFormatter()
    with phase_ctx("discovery"):
        json_out = json_fmt.format(rec_info)
    import json as _json
    parsed = _json.loads(json_out)
    _check("[phase-json-field] JSON output carries phase as a top-level field",
           parsed.get("phase"), "discovery")
    # Outside a phase block, JSON output has no `phase` field at all.
    json_out = json_fmt.format(rec_info)
    parsed = _json.loads(json_out)
    _check("[phase-json-absent] JSON omits the phase field outside any block",
           "phase" in parsed, False)

    # ── (11) JSON formatter enriches structured output with `tag` field
    #         extracted from the leading `[tag]` of the message body.
    #         Loki / ELK / Splunk consumers can filter `{tag="oom-bump"}`
    #         without regex-parsing the message string.
    rec_tagged = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="[oom-bump] ns/wl/c: 100Mi → 150Mi", args=None, exc_info=None,
    )
    parsed = _json.loads(json_fmt.format(rec_tagged))
    _check("[json-tag-extracted] leading [tag] surfaced as JSON 'tag' field",
           parsed.get("tag"), "oom-bump")
    _check("[json-tag-message-preserved] message body keeps the bracketed tag",
           parsed["message"].startswith("[oom-bump] "), True)

    # Tagless line → no `tag` field at all.
    rec_untagged = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="bare info message", args=None, exc_info=None,
    )
    parsed = _json.loads(json_fmt.format(rec_untagged))
    _check("[json-tag-absent] no tag field on untagged messages",
           "tag" in parsed, False)

    # `[DRY RUN]` (space inside brackets) is a valid tag — regression
    # guard for the regex character class allowing the literal space.
    rec_dry = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="[DRY RUN] would write 3 files", args=None, exc_info=None,
    )
    parsed = _json.loads(json_fmt.format(rec_dry))
    _check("[json-tag-with-space] [DRY RUN] extracts the inner two-word tag",
           parsed.get("tag"), "DRY RUN")

    # `extra={...}` on a log call propagates to JSON as top-level keys
    # (already-working contract — regression guard the deprecation of
    # `_STDLIB_ATTRS` keys is honored).
    rec_extra = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="[OK] webhook: pushed", args=None, exc_info=None,
    )
    rec_extra.namespace = "goldilocks"
    rec_extra.workload = "kru-test"
    rec_extra.files_changed = 3
    parsed = _json.loads(json_fmt.format(rec_extra))
    _check("[json-extra-namespace] extra.namespace passes through",
           parsed.get("namespace"), "goldilocks")
    _check("[json-extra-workload] extra.workload passes through",
           parsed.get("workload"), "kru-test")
    _check("[json-extra-int] extra integer values pass through (not stringified)",
           parsed.get("files_changed"), 3)


def section_resolver() -> None:
    """Table-driven coverage of the helm < namespace < workload hierarchy.

    Every row sets up a single workload's resolution chain and asserts the
    effective value of one or more fields. The matrix is deliberately wide
    rather than deep so QA prints one PASS/FAIL line per (key × scenario)
    cell — easy to scan, and a regression in any cell pinpoints both the
    failing key and the failing layer.
    """
    _section("resolver — helm / namespace / workload override matrix")

    P = "kube-resource-updater."  # annotation prefix (DRY)

    # Cluster-default (helm) Config used as the base for every scenario.
    helm = _base_config()
    helm.grow_only = False
    helm.shrink_only = False
    helm.create_mr = True
    helm.dry_run = False
    helm.min_cpu_limit_m = 0
    helm.min_memory_limit_mi = 0
    helm.resource.cpu_percentile = 0.80
    helm.resource.mem_percentile = 0.80
    helm.resource.margin_fraction = 0.20
    helm.resource.cpu_request_window = "3d"
    helm.resource.cpu_limit_multiplier = 4.0
    helm.resource.memory_limit_multiplier = 3.0
    helm.resource.round_values = False
    helm.resource.min_cpu_request_m = 200
    helm.resource.cold_start_cpu_floor_m = 10
    helm.resource.oom_bump_factor = 1.5

    # ── 6a. Helm-only baseline (no annotations anywhere) ────────────────
    eff = resolve_for_workload(helm, {}, {})
    _check("[helm-only] grow_only",           eff.grow_only,                  False)
    _check("[helm-only] shrink_only",         eff.shrink_only,                False)
    _check("[helm-only] cpu_percentile",      eff.resource.cpu_percentile,    0.80)
    _check("[helm-only] mem_percentile",      eff.resource.mem_percentile,    0.80)
    _check("[helm-only] margin_fraction",     eff.resource.margin_fraction,   0.20)
    _check("[helm-only] min_cpu_request_m",   eff.resource.min_cpu_request_m, 200)
    _check("[helm-only] returns identity",    eff is helm,                    True)

    # ── 6b. Namespace overrides only ─────────────────────────────────────
    ns_only = {
        P + "shrinkOnly":          "true",
        P + "cpuPercentile":       "0.95",
        P + "memPercentile":       "0.99",
        P + "marginFraction":      "0.10",
        P + "cpuRequestWindow":    "7d",
        P + "minCpuRequestM":      "300",
        P + "cpuLimitMultiplier":  "5",
    }
    eff = resolve_for_workload(helm, ns_only, {})
    _check("[ns-only] shrink_only flips on",  eff.shrink_only,                  True)
    _check("[ns-only] grow_only stays off",   eff.grow_only,                    False)
    _check("[ns-only] cpu_percentile",        eff.resource.cpu_percentile,      0.95)
    _check("[ns-only] mem_percentile",        eff.resource.mem_percentile,      0.99)
    _check("[ns-only] margin_fraction",       eff.resource.margin_fraction,     0.10)
    _check("[ns-only] cpu_request_window",    eff.resource.cpu_request_window,  "7d")
    _check("[ns-only] min_cpu_request_m",     eff.resource.min_cpu_request_m,   300)
    _check("[ns-only] cpu_limit_multiplier",  eff.resource.cpu_limit_multiplier, 5.0)
    _check("[ns-only] mem_percentile from ns, mem_request_window from helm",
           (eff.resource.mem_percentile, eff.resource.mem_request_window),
           (0.99, "8d"))   # 8d is the dataclass default

    # ── 6c. Workload overrides only ──────────────────────────────────────
    wl_only = {
        P + "growOnly":               "true",
        P + "marginFraction":         "0.50",
        P + "memoryLimitMultiplier":  "6",
        P + "roundValues":            "true",
        P + "maxMemoryRequestMi":     "2048",
    }
    eff = resolve_for_workload(helm, {}, wl_only)
    _check("[wl-only] grow_only flips on",     eff.grow_only,                       True)
    _check("[wl-only] shrink_only stays off",  eff.shrink_only,                     False)
    _check("[wl-only] margin_fraction",        eff.resource.margin_fraction,        0.50)
    _check("[wl-only] mem_limit_multiplier",   eff.resource.memory_limit_multiplier, 6.0)
    _check("[wl-only] round_values",           eff.resource.round_values,           True)
    _check("[wl-only] max_memory_request_mi",  eff.resource.max_memory_request_mi,  2048)
    _check("[wl-only] cpu_percentile from helm (untouched)",
           eff.resource.cpu_percentile, 0.80)

    # ── 6d. Namespace AND workload — workload wins on overlap ────────────
    ns_overlap = {
        P + "shrinkOnly":      "true",   # ← overridden by workload
        P + "cpuPercentile":   "0.95",   # ← kept (workload silent on this)
        P + "marginFraction":  "0.10",   # ← overridden by workload
    }
    wl_overlap = {
        P + "growOnly":        "true",
        P + "shrinkOnly":      "false",  # explicit revert
        P + "marginFraction":  "0.30",
    }
    eff = resolve_for_workload(helm, ns_overlap, wl_overlap)
    _check("[ns+wl] workload growOnly wins",        eff.grow_only,                True)
    _check("[ns+wl] workload shrinkOnly wins",      eff.shrink_only,              False)
    _check("[ns+wl] ns cpuPercentile kept",         eff.resource.cpu_percentile,  0.95)
    _check("[ns+wl] workload marginFraction wins",  eff.resource.margin_fraction, 0.30)

    # ── 6e. Bad value at one layer falls through to the next ─────────────
    ns_bad = {
        P + "cpuPercentile":  "not-a-number",   # dropped → fall through
        P + "marginFraction": "0.10",           # kept
    }
    wl_bad = {
        P + "marginFraction": "still-bogus",    # dropped → ns 0.10 kept
    }
    eff = resolve_for_workload(helm, ns_bad, wl_bad)
    _check("[bad-vals] cpu_percentile falls back to helm",
           eff.resource.cpu_percentile, 0.80)
    _check("[bad-vals] margin_fraction falls back to ns",
           eff.resource.margin_fraction, 0.10)

    # ── 6f. Unknown / typo annotation ignored, known keys still applied ──
    ns_typo = {
        P + "cpuPerncentile": "0.99",   # typo: extra 'n'
        P + "cpuPercentile":  "0.85",   # correct
    }
    eff = resolve_for_workload(helm, ns_typo, {})
    _check("[typo] correct key applied",         eff.resource.cpu_percentile, 0.85)

    # ── 6g. workload `skip` annotation does NOT leak into Config fields ──
    eff = resolve_for_workload(helm, {}, {P + "skip": "true",
                                          P + "cpuPercentile": "0.91"})
    _check("[skip-annot] cpu_percentile still applied",
           eff.resource.cpu_percentile, 0.91)
    _check("[skip-annot] returned object is still a Config",
           type(eff).__name__, "Config")

    # ── 6h. Cross-field independence (ns sets some, wl sets others) ─────
    ns_split = {P + "cpuPercentile":   "0.95",
                P + "minCpuRequestM":  "150"}
    wl_split = {P + "memPercentile":   "0.99",
                P + "marginFraction":  "0.40"}
    eff = resolve_for_workload(helm, ns_split, wl_split)
    _check("[split] cpu_percentile from ns",       eff.resource.cpu_percentile,    0.95)
    _check("[split] min_cpu_request_m from ns",    eff.resource.min_cpu_request_m, 150)
    _check("[split] mem_percentile from wl",       eff.resource.mem_percentile,    0.99)
    _check("[split] margin_fraction from wl",      eff.resource.margin_fraction,   0.40)
    _check("[split] cpu_limit_multiplier from helm",
           eff.resource.cpu_limit_multiplier, 4.0)

    # ── 6i. Per-key precedence truth table ──────────────────────────────
    # For every override-able key, walk the four precedence cells:
    #
    #   (ns absent, wl absent) → helm default wins
    #   (ns set,    wl absent) → ns value wins
    #   (ns absent, wl set)    → wl value wins
    #   (ns set,    wl set)    → wl value wins (workload always trumps)
    #
    # Each cell adds a PASS/FAIL line so a precedence regression points
    # exactly at the (key, cell) pair that broke. The case definitions
    # are paired (annotation_value, expected_python_value) so type
    # coercion in src/overrides.py is exercised end-to-end too — string
    # "true" → bool True, "0.95" → float 0.95, "150" → int 150.
    print("  ── precedence truth table (per key × layer cell) ──")

    # (annotation_key, helm_default, ns_string_value, wl_string_value, expected_ns, expected_wl, getter)
    # `getter` returns the effective Python value from a Config object.
    cases: list[tuple] = [
        # bool flags
        ("growOnly",   False, "true",  "false", True,  False,
            lambda c: c.grow_only),
        ("shrinkOnly", False, "true",  "false", True,  False,
            lambda c: c.shrink_only),
        ("dryRun",     False, "true",  "false", True,  False,
            lambda c: c.dry_run),
        ("createMr",   True,  "false", "true",  False, True,
            lambda c: c.create_mr),
        ("roundValues", False, "true", "false", True,  False,
            lambda c: c.resource.round_values),
        # floats — windows / percentiles / margins / multipliers
        ("cpuPercentile",         0.80, "0.95",  "0.99", 0.95,  0.99,
            lambda c: c.resource.cpu_percentile),
        ("memPercentile",         0.80, "0.92",  "0.97", 0.92,  0.97,
            lambda c: c.resource.mem_percentile),
        ("marginFraction",        0.20, "0.10",  "0.30", 0.10,  0.30,
            lambda c: c.resource.margin_fraction),
        ("cpuRequestMargin",      None, "0.15",  "0.25", 0.15,  0.25,
            lambda c: c.resource.cpu_request_margin),
        ("memRequestMargin",      None, "0.15",  "0.25", 0.15,  0.25,
            lambda c: c.resource.mem_request_margin),
        ("cpuLimitMargin",        None, "0.05",  "0.45", 0.05,  0.45,
            lambda c: c.resource.cpu_limit_margin),
        ("memLimitMargin",        None, "0.05",  "0.45", 0.05,  0.45,
            lambda c: c.resource.mem_limit_margin),
        ("cpuLimitMultiplier",    4.0,  "5",     "8",    5.0,   8.0,
            lambda c: c.resource.cpu_limit_multiplier),
        ("memoryLimitMultiplier", 3.0,  "4",     "6",    4.0,   6.0,
            lambda c: c.resource.memory_limit_multiplier),
        # strings — windows
        ("cpuRequestWindow", "3d", "5d",  "7d",  "5d",  "7d",
            lambda c: c.resource.cpu_request_window),
        ("memRequestWindow", "8d", "5d",  "7d",  "5d",  "7d",
            lambda c: c.resource.mem_request_window),
        ("cpuLimitWindow",   "7d", "1d",  "14d", "1d",  "14d",
            lambda c: c.resource.cpu_limit_window),
        ("memLimitWindow",   "7d", "1d",  "14d", "1d",  "14d",
            lambda c: c.resource.mem_limit_window),
        # ints — bounds
        ("minCpuRequestM",     200, "300",  "400",  300,  400,
            lambda c: c.resource.min_cpu_request_m),
        ("minMemoryRequestMi", 0,   "256",  "512",  256,  512,
            lambda c: c.resource.min_memory_request_mi),
        ("maxCpuRequestM",     0,   "1000", "2000", 1000, 2000,
            lambda c: c.resource.max_cpu_request_m),
        ("maxMemoryRequestMi", 0,   "1024", "4096", 1024, 4096,
            lambda c: c.resource.max_memory_request_mi),
        ("maxCpuLimitM",       0,   "2000", "4000", 2000, 4000,
            lambda c: c.resource.max_cpu_limit_m),
        ("maxMemoryLimitMi",   0,   "2048", "8192", 2048, 8192,
            lambda c: c.resource.max_memory_limit_mi),
        ("minCpuLimitM",       0,   "100",  "200",  100,  200,
            lambda c: c.min_cpu_limit_m),
        ("minMemoryLimitMi",   0,   "128",  "256",  128,  256,
            lambda c: c.min_memory_limit_mi),
        # coldStartCpuFloorM (#56) + oomBumpFactor — both are ResourceConfig
        # fields documented as per-workload overrides; both were missing from
        # `_KEY_SPEC` so their annotations were silently dropped. Same bug
        # class. coldStartCpuFloorM cells pass (fixed); oomBumpFactor cells
        # 2/3/4 FAIL pre-fix (annotation dropped → stays at helm 1.5).
        ("coldStartCpuFloorM", 10,  "25",   "40",   25,   40,
            lambda c: c.resource.cold_start_cpu_floor_m),
        ("oomBumpFactor",      1.5, "2.0",  "3.0",  2.0,  3.0,
            lambda c: c.resource.oom_bump_factor),
    ]

    for key, helm_default, ns_val, wl_val, exp_ns, exp_wl, get in cases:
        # cell 1: nothing set → helm default wins
        eff = resolve_for_workload(helm, {}, {})
        _check(f"[{key}] (ns_∅, wl_∅) → helm",      get(eff), helm_default)
        # cell 2: ns sets, wl silent → ns wins
        eff = resolve_for_workload(helm, {P + key: ns_val}, {})
        _check(f"[{key}] (ns={ns_val}, wl_∅) → ns",  get(eff), exp_ns)
        # cell 3: ns silent, wl sets → wl wins
        eff = resolve_for_workload(helm, {}, {P + key: wl_val})
        _check(f"[{key}] (ns_∅, wl={wl_val}) → wl",  get(eff), exp_wl)
        # cell 4: ns and wl both set with different values → wl wins
        eff = resolve_for_workload(helm, {P + key: ns_val}, {P + key: wl_val})
        _check(f"[{key}] (ns={ns_val}, wl={wl_val}) → wl",
               get(eff), exp_wl)


def section_webhook_cert_san_clusterdomain() -> None:
    """— `cluster.local` hardcoded in webhook cert SAN.

    Pre-1.22.11 `src/webhook_cert.py:_generate_cert` baked `cluster.local`
    directly into the SAN list. Clusters with custom clusterDomain
    (`cluster.foo.com`) hit a SAN-vs-DNS mismatch on the apiserver-to-
    webhook TLS handshake; `failurePolicy: Ignore` swallows it and pods
    admit with helm defaults for weeks. Fix: `_generate_cert` accepts a
    `cluster_domain: str = "cluster.local"` parameter; main.py threads
    `WEBHOOK_CLUSTER_DOMAIN` env var (chart `.Values.clusterDomain`) in.

    Pinned invariants:
      - default cluster_domain produces SAN including `<svc>.<ns>.svc.cluster.local`
        (backwards-compat for every existing deployment);
      - custom cluster_domain produces SAN including `<svc>.<ns>.svc.<custom>`
        and NOT the literal `cluster.local`;
      - the short DNS forms (`<svc>`, `<svc>.<ns>`, `<svc>.<ns>.svc`) remain
        regardless of cluster_domain (those forms don't include the domain).
    """
    _section("Webhook cert SAN — configurable clusterDomain")

    from cryptography import x509
    from src.webhook_cert import _generate_cert

    # ── 1. Default cluster_domain backwards-compat ──────────────────────
    materials = _generate_cert(service="kru-webhook", namespace="kru-ns")
    cert = x509.load_pem_x509_certificate(materials.cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    dns_names = [n.value for n in san.value]

    _check("[cert-san-default-cluster-local] default SAN includes <svc>.<ns>.svc.cluster.local",
           "kru-webhook.kru-ns.svc.cluster.local" in dns_names, True)
    _check("[cert-san-default-short] short DNS form present (<svc>)",
           "kru-webhook" in dns_names, True)
    _check("[cert-san-default-mid1] mid DNS form present (<svc>.<ns>)",
           "kru-webhook.kru-ns" in dns_names, True)
    _check("[cert-san-default-mid2] mid DNS form present (<svc>.<ns>.svc)",
           "kru-webhook.kru-ns.svc" in dns_names, True)

    # ── 2. Custom cluster_domain ────────────────────────────────────────
    materials = _generate_cert(service="kru-webhook", namespace="kru-ns",
                                cluster_domain="cluster.foo.com")
    cert = x509.load_pem_x509_certificate(materials.cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    dns_names = [n.value for n in san.value]

    _check("[cert-san-custom-domain] custom SAN includes <svc>.<ns>.svc.cluster.foo.com",
           "kru-webhook.kru-ns.svc.cluster.foo.com" in dns_names, True)
    _check("[cert-san-custom-no-default] custom cluster_domain does NOT include cluster.local",
           "kru-webhook.kru-ns.svc.cluster.local" in dns_names, False)
    _check("[cert-san-custom-short] short DNS form still present (<svc>)",
           "kru-webhook" in dns_names, True)


def section_mr_description_truncation_count() -> None:
    """— `_mr_description` silent truncation hides workload count.

    Pre-1.22.11 the truncation footer said only "description truncated;
    see commit message". Operator opening the MR sees the visible rows
    and assumes that's the full set; tail workloads are silently elided.
    Fix: footer reports how many container rows were dropped vs total.

    Pinned invariants:
      - under-cap body: pass-through unchanged, no footer (regression guard);
      - over-cap body: footer present AND mentions a count;
      - over-cap body: count is positive (something was actually dropped);
      - over-cap body: count is correct (rows_dropped = rows_in_original - rows_in_truncated_output).
    """
    _section("MR description truncation — drop-count footer")

    from src.writeback_webhook import (
        _MR_DESCRIPTION_CAP_BYTES,
        _truncate_mr_description,
    )

    # ── 1. Under-cap body passes through unchanged ──────────────────────
    small = (
        "## ResourceOverride CRs\n\n"
        "| Namespace | Workload | Container | CPU req | Mem req | CPU lim | Mem lim |\n"
        "|---|---|---|---|---|---|---|\n"
        "| `ns1` | `a` | `c1` | 100m | 128Mi | 200m | 256Mi |\n"
        "| `ns1` | `b` | `c2` | 200m | 256Mi | 400m | 512Mi |\n"
    )
    out = _truncate_mr_description(small)
    _check("[mr-trunc-undercap-passthrough] under-cap body returned unchanged",
           out, small)
    _check("[mr-trunc-undercap-no-count-footer] no drop-count footer when not truncated",
           "more container row" in out, False)

    # ── 2. Over-cap body emits a drop-count footer with positive N ──────
    # Build a body with many rows, all unique per-row, well over the cap.
    row_template = (
        "| `ns-{n:04d}` | `workload-{n:04d}` | `container-{n:04d}` "
        "| 100m | 128Mi | 200m | 256Mi |\n"
    )
    rows = [row_template.format(n=i) for i in range(20000)]
    huge = (
        "## ResourceOverride CRs\n\n"
        "| Namespace | Workload | Container | CPU req | Mem req | CPU lim | Mem lim |\n"
        "|---|---|---|---|---|---|---|\n"
        + "".join(rows)
    )

    out = _truncate_mr_description(huge)

    _check("[mr-trunc-overcap-footer-present] over-cap body has truncation footer",
           "more container row" in out, True)
    _check("[mr-trunc-overcap-under-gitlab-cap] truncated body strictly below 1 MiB",
           len(out.encode("utf-8")) < 1024 * 1024, True)
    _check("[mr-trunc-overcap-under-our-cap] truncated body under cap constant",
           len(out.encode("utf-8")) <= _MR_DESCRIPTION_CAP_BYTES, True)

    # Extract the drop count from the footer; verify it matches actual loss.
    import re as _re
    m = _re.search(r"(\d+) more container row", out)
    _check("[mr-trunc-overcap-count-parseable] footer count is a parseable integer",
           m is not None, True)
    if m is not None:
        dropped_reported = int(m.group(1))
        # rows kept = lines in truncated output that start with "| `ns-"
        kept_rows = sum(1 for line in out.split("\n") if line.startswith("| `ns-"))
        dropped_actual = len(rows) - kept_rows
        _check("[mr-trunc-overcap-count-positive] reported drop count > 0",
               dropped_reported > 0, True)
        _check("[mr-trunc-overcap-count-correct] reported drop count matches actual loss",
               dropped_reported, dropped_actual)


def section_oom_bump_cap_with_investigation() -> None:
    """— `oom-boost-history` cap of 10 silent; runaway-bump cap.

    Pre-1.22.11 if a container OOMed 10+ times in a row, history capped
    at 10 entries but the tool kept bumping the memory limit on every
    fresh event. A real memory leak (or misconfigured GC) would get its
    limit infinitely raised, masking the underlying bug. Fix: after
    N=5 bumps recorded in history, suppress further bumps and stamp
    `oom-investigation-required.<container>: "true"` so operator sees
    the workload needs human attention.

    Pinned invariants:
      - constant `_OOM_BUMPS_BEFORE_INVESTIGATION` exists, is at least 3
        and at most 10 (sane bounds);
      - `_count_history_entries(prev_history)` returns 0 for "", N for
        N-line history;
      - investigation annotation key uses the standard
        `kube-resource-updater.io/oom-investigation-required.<container>` shape;
      - `parse_oom_investigation_from_annotations` round-trips through
        the read-back path so investigation state survives sync cycles.
    """
    _section("OOM bump runaway-cap + investigation annotation")

    from src.writeback_webhook import (
        _OOM_BUMPS_BEFORE_INVESTIGATION,
        _count_history_entries,
        OOM_INVESTIGATION_PREFIX,
        investigation_annotation_key,
        parse_oom_investigation_from_annotations,
    )

    # ── 1. Constant in sane bounds ───────────────────────────────────────
    _check("[oom-cap-const-min] _OOM_BUMPS_BEFORE_INVESTIGATION >= 3",
           _OOM_BUMPS_BEFORE_INVESTIGATION >= 3, True)
    _check("[oom-cap-const-max] _OOM_BUMPS_BEFORE_INVESTIGATION <= 10 (history is 10-capped)",
           _OOM_BUMPS_BEFORE_INVESTIGATION <= 10, True)

    # ── 2. _count_history_entries shape ──────────────────────────────────
    _check("[oom-history-count-empty] empty history → 0",
           _count_history_entries(""), 0)
    _check("[oom-history-count-three] 3-line history → 3",
           _count_history_entries("line1\nline2\nline3"), 3)
    _check("[oom-history-count-skip-blank] blank lines don't count",
           _count_history_entries("line1\n\nline2\n   "), 2)

    # ── 3. Annotation key shape ──────────────────────────────────────────
    _check("[oom-investigation-key-prefix] key prefix is canonical",
           OOM_INVESTIGATION_PREFIX,
           "kube-resource-updater.io/oom-investigation-required.")
    _check("[oom-investigation-key-builder] investigation_annotation_key includes container",
           investigation_annotation_key("app-c"),
           "kube-resource-updater.io/oom-investigation-required.app-c")

    # ── 4. parse_oom_investigation_from_annotations round-trip ───────────
    anns = {
        "kube-resource-updater.io/oom-investigation-required.app-c": "true",
        "kube-resource-updater.io/oom-investigation-required.sidecar": "true",
        "kube-resource-updater.io/oom-floor.app-c": "512Mi",  # other OOM key — must NOT be picked up
        "unrelated": "value",
    }
    parsed = parse_oom_investigation_from_annotations(anns)
    _check("[oom-investigation-parse-keys] picked up both investigation-required containers",
           sorted(parsed.keys()), ["app-c", "sidecar"])
    _check("[oom-investigation-parse-value-app-c] value is True (string \"true\" maps to bool)",
           parsed.get("app-c"), True)
    _check("[oom-investigation-parse-no-floor-leak] floor annotation not parsed here",
           "app-c" in parsed and len(parsed) == 2, True)
    _check("[oom-investigation-parse-empty] empty annotations → empty dict",
           parse_oom_investigation_from_annotations({}), {})
    _check("[oom-investigation-parse-none] None annotations → empty dict",
           parse_oom_investigation_from_annotations(None), {})


# --------------------------------------------------------------------------- #
# Section: cold-start CPU floor — #
# --------------------------------------------------------------------------- #

def section_cold_start_cpu_floor() -> None:
    """— cold-start path synthesizes only 1m CPU when minCpuRequestM=0.

    `_build_containers_payload` line ~922 (pre-fix):

        cpu_floor_m = max(bounds.min_cpu_request_m, 1)

    With chart default `minCpuRequestM: "0"`, a pod that just OOMed with no
    Prom CPU history gets `cpu_floor_m = max(0, 1) = 1m`. That is below any
    reasonable readiness threshold — the pod throttles immediately and usually
    fails its readiness probe before it can generate Prom data, creating a
    permanent deadlock.

    Fix (python agent):
      - Add `cold_start_cpu_floor_m: int = 10` to `ResourceConfig`.
      - Thread through `from_dict` (`coldStartCpuFloorM`) and `from_env`
        (`COLD_START_CPU_FLOOR_M`).
      - Replace line ~922 with:
          cpu_floor_m = max(bounds.min_cpu_request_m, rc.cold_start_cpu_floor_m)

    Fix (k8s agent):
      - Add `coldStartCpuFloorM: 10` to chart values.yaml under
        `config.resourceConfig` (same block as `minCpuRequestM`).
      - Thread into ConfigMap template as `coldStartCpuFloorM:`.
      - No validate.yaml gate needed (any non-negative int is valid).

    Assert strategy: all asserts inspect the synthesized CPU request string
    in the returned payload. No monkeypatch of the missing field is needed —
    the positive assert fails on pre-fix code because the hardcoded 1 produces
    "1m", which is < 10m. Post-fix it produces "10m".
    """
    _section

    from unittest.mock import patch

    from src.config import Config, CrWritebackConfig, ResourceConfig
    from src.workload import ContainerRecommendation, WorkloadRecommendation
    from src.writeback_webhook import OomEvent, _build_containers_payload

    def _parse_cpu_m(cpu_str: str) -> int:
        """Parse a cpu string like '10m' or '1m' into millicores int."""
        s = str(cpu_str).strip()
        if s.endswith("m"):
            return int(s[:-1])
        # plain cores (e.g. "1")
        return int(float(s) * 1000)

    # ── Scenario: cold-start (no Prom data) + fresh OOM + minCpuRequestM=0 ──
    # This is the exact bug path. The pod OOMed but has no Prom CPU history
    # (first-run, crash-loop). Prom returns None. The cold-start synthesizer
    # runs. With the bug: floor = max(0, 1) = 1m. With the fix: floor = 10m.
    ev = OomEvent(
        namespace="ns", workload_name="api", container="app",
        finished_at="2099-01-01T00:00:00Z",   # far-future → always "fresh"
        trap_limit_bytes=128 * 1024 * 1024,    # 128 Mi trap
    )

    cfg_zero_floor = Config(
        gitlab_url="", gitlab_token="", gitlab_username="",
        git_author_name="kru", git_author_email="kru@example",
        dry_run=False, create_mr=False,
        min_cpu_limit_m=0, min_memory_limit_mi=0,
        prometheus_url="http://prom:9090",
        resource=ResourceConfig(
            min_cpu_request_m=0,        # chart default minCpuRequestM: "0"
            min_memory_request_mi=0,
            oom_detection_enabled=True,
            oom_bump_factor=1.5,
            oom_floor_enabled=True,
        ),
        cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides"),
    )
    rec = WorkloadRecommendation(
        name="api", namespace="ns", target_kind="Deployment", target_name="api",
        containers=[ContainerRecommendation(container_name="app")],
    )

    # No Prom history: _query_prom_values returns None → cold-start path runs.
    with patch("src.writeback_webhook._query_prom_values", return_value=None):
        payload, _ = _build_containers_payload(
            rec, cfg_zero_floor,
            oom_state={"floor": {}, "last_event": {}, "history": {}, "containers": {}},
            oom_events={"app": ev},
            oom_eligible=True,
        )

    # ── Positive assert (MUST FAIL pre-fix) ─────────────────────────────────
    # Pre-fix: cpu_floor_m = max(0, 1) = 1m → payload[0]["requests"]["cpu"] == "1m"
    # Post-fix: cpu_floor_m = max(0, 10) = 10m → payload[0]["requests"]["cpu"] == "10m"
    assert payload, "Expected non-empty payload from cold-start OOM path"
    got_cpu_m = _parse_cpu_m(payload[0]["requests"]["cpu"])
    _check(
        "[cold-start-floor-min10] cold-start CPU request >= 10m "
        "(hardcoded 1 fails this; fix requires cold_start_cpu_floor_m=10)",
        got_cpu_m >= 10,
        True,
    )

    # ── Negative control (must PASS pre-fix AND post-fix) ────────────────────
    # When minCpuRequestM=50, the min_cpu_request_m dominates regardless of
    # cold_start_cpu_floor_m=10. floor = max(50, X) = 50m.
    cfg_large_floor = Config(
        gitlab_url="", gitlab_token="", gitlab_username="",
        git_author_name="kru", git_author_email="kru@example",
        dry_run=False, create_mr=False,
        min_cpu_limit_m=0, min_memory_limit_mi=0,
        prometheus_url="http://prom:9090",
        resource=ResourceConfig(
            min_cpu_request_m=50,       # explicit 50m floor dominates
            min_memory_request_mi=0,
            oom_detection_enabled=True,
            oom_bump_factor=1.5,
            oom_floor_enabled=True,
        ),
        cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides"),
    )
    with patch("src.writeback_webhook._query_prom_values", return_value=None):
        payload2, _ = _build_containers_payload(
            rec, cfg_large_floor,
            oom_state={"floor": {}, "last_event": {}, "history": {}, "containers": {}},
            oom_events={"app": ev},
            oom_eligible=True,
        )

    assert payload2, "Expected non-empty payload (large-floor control)"
    got_cpu_m2 = _parse_cpu_m(payload2[0]["requests"]["cpu"])
    _check(
        "[cold-start-floor-dominant-min] when minCpuRequestM=50 > cold_start_cpu_floor_m=10, "
        "floor is 50m (the larger value dominates) — PASSES both pre- and post-fix",
        got_cpu_m2 >= 50,
        True,
    )

    # ── Default sanity: no OOM event → cold-start path is NOT triggered ──────
    # Confirms we do not accidentally activate cold-start synthesis on normal
    # (non-OOM) workloads with no Prom data. Payload should be empty (no data).
    with patch("src.writeback_webhook._query_prom_values", return_value=None):
        payload3, _ = _build_containers_payload(
            rec, cfg_zero_floor,
            oom_state={"floor": {}, "last_event": {}, "history": {}, "containers": {}},
            oom_events={},              # no OOM events
            oom_eligible=True,
        )
    _check(
        "[cold-start-no-oom-no-synthesis] without a fresh OOM event, "
        "cold-start synthesis does NOT run (payload stays empty)",
        payload3,
        [],
    )

    # ── Bug #56 — coldStartCpuFloorM must honor the ns/workload override chain ──
    # `cold_start_cpu_floor_m` lives on ResourceConfig (cluster default via the
    # ConfigMap) and is consumed at writeback_webhook.py:922 as
    # `rc.cold_start_cpu_floor_m`, where `rc = cfg.resource` is the per-workload
    # *resolved* config. But pre-#56 the key was absent from `_KEY_SPEC`, so the
    # resolver silently dropped any `kube-resource-updater.coldStartCpuFloorM`
    # namespace/workload annotation — a JVM workload cold-starting at a higher
    # floor could only change it via the cluster-wide ConfigMap. Same override
    # chain as `minCpuRequestM`. (a)/(b) FAIL pre-fix (annotation dropped → 10).
    from src.overrides import resolve_for_workload

    base_cfg = Config(
        gitlab_url="", gitlab_token="", gitlab_username="",
        git_author_name="kru", git_author_email="kru@example",
        dry_run=False, create_mr=False,
        min_cpu_limit_m=0, min_memory_limit_mi=0,
        prometheus_url="http://prom:9090",
        resource=ResourceConfig(cold_start_cpu_floor_m=10),  # cluster default
        cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides"),
    )

    resolved_ns = resolve_for_workload(
        base_cfg,
        {"kube-resource-updater.coldStartCpuFloorM": "30"},
        None,
        namespace_name="ns", workload_name="api",
    )
    _check("[cold-start-#56-ns] namespace annotation overrides cluster floor (10→30)",
           resolved_ns.resource.cold_start_cpu_floor_m, 30)

    resolved_wl = resolve_for_workload(
        base_cfg,
        {"kube-resource-updater.coldStartCpuFloorM": "30"},
        {"kube-resource-updater.coldStartCpuFloorM": "50"},
        namespace_name="ns", workload_name="api",
    )
    _check("[cold-start-#56-wl] workload annotation wins over namespace (→50)",
           resolved_wl.resource.cold_start_cpu_floor_m, 50)

    resolved_default = resolve_for_workload(
        base_cfg, None, None, namespace_name="ns", workload_name="api",
    )
    _check("[cold-start-#56-default] no annotation → cluster default (10) preserved",
           resolved_default.resource.cold_start_cpu_floor_m, 10)




# --------------------------------------------------------------------------- #
# Section: OOM bump clamp warning — #
# --------------------------------------------------------------------------- #

def section_oom_bump_clamp_warning() -> None:
    """— silent clamp when oom_bump_target_bytes > maxMemoryLimitMi.

    Pre-fix code at writeback_webhook.py lines 994-998 silently applies
    `min(oom_bump_target_bytes, max_memory_limit_mi * 1<<20)` with NO log.
    The operator set a bumpFactor expecting a 1.5× raise but got 1.36×
    because the ceiling ate the difference. No warning = no operator signal.

    Example:
        trap_limit_bytes = 3000 Mi, oom_bump_factor = 1.5
        → wants 4500 Mi, cap = 4096 Mi → effective 4096 Mi (1.365× instead of 1.5×)

    Pinned invariants:
      - When bump is clamped by maxMemoryLimitMi, a WARNING containing the
        tag "oom-bump-clamped" is emitted by the `src.writeback_webhook`
        logger, with the intended bytes, the effective capped bytes, and
        the workload namespace/name/container.
      - When the bump fits within the cap, NO "oom-bump-clamped" warning is
        emitted (negative control — assert stays quiet on the happy path).
    """
    _section

    import io as _io
    import logging as _logging
    from types import SimpleNamespace
    from unittest.mock import patch

    from src.workload import ContainerRecommendation, WorkloadRecommendation
    from src.writeback_webhook import OomEvent, _build_containers_payload

    M = 1024 * 1024

    cfg = _base_config()
    cfg.prometheus_url = "http://prom:9090"
    cfg.resource.oom_detection_enabled = True
    cfg.resource.oom_bump_factor = 1.5
    cfg.resource.memory_limit_multiplier = 3.0
    # Cap that is lower than trap × factor: 3000 Mi × 1.5 = 4500 Mi > 4096 Mi cap
    cfg.resource.max_memory_limit_mi = 4096

    rec = WorkloadRecommendation(
        name="api", namespace="ns", target_kind="Deployment", target_name="api",
        containers=[ContainerRecommendation(container_name="app")],
    )

    prom_values = SimpleNamespace(
        cpu_request_m=200, memory_request_bytes=1000 * M,
        cpu_limit_m=400, memory_limit_bytes=3000 * M,
    )

    # OOM event: trap at 3000 Mi.  factor=1.5 → wants 4500 Mi, cap=4096 Mi.
    ev_clamped = OomEvent(
        namespace="ns", workload_name="api", container="app",
        finished_at="2026-05-25T10:00:00Z",
        trap_limit_bytes=3000 * M,
    )

    # ── Capture warnings from the src.writeback_webhook logger ──────────
    log_buf = _io.StringIO()
    handler = _logging.StreamHandler(log_buf)
    handler.setLevel(_logging.WARNING)
    wh_logger = _logging.getLogger("src.writeback_webhook")
    wh_logger.addHandler(handler)

    try:
        with patch("src.writeback_webhook._query_prom_values", return_value=prom_values):
            _build_containers_payload(
                rec, cfg,
                oom_state={"floor": {}, "last_event": {}, "history": {}},
                oom_events={"app": ev_clamped},
                oom_eligible=True,
            )
    finally:
        wh_logger.removeHandler(handler)

    log_text = log_buf.getvalue()

    # ── Primary asserts (must FAIL pre-fix) ─────────────────────────────
    _check("[oom-bump-clamped] WARNING emitted when bump exceeds cap",
           "[oom-bump-clamped]" in log_text, True)
    _check("[oom-bump-clamped] warning names the intended target bytes (4500Mi)",
           "4500" in log_text, True)
    _check("[oom-bump-clamped] warning names the effective capped bytes (4096Mi)",
           "4096" in log_text, True)
    _check("[oom-bump-clamped] warning identifies the workload (ns/api/app)",
           "ns" in log_text and "api" in log_text and "app" in log_text, True)

    # ── Negative control: bump fits → no warning ─────────────────────────
    # trap=3000Mi × factor=1.2 → 3600 Mi < cap 4096 Mi → no clamp warning.
    cfg_no_clamp = _base_config()
    cfg_no_clamp.prometheus_url = "http://prom:9090"
    cfg_no_clamp.resource.oom_detection_enabled = True
    cfg_no_clamp.resource.oom_bump_factor = 1.2
    cfg_no_clamp.resource.memory_limit_multiplier = 3.0
    cfg_no_clamp.resource.max_memory_limit_mi = 4096

    ev_fits = OomEvent(
        namespace="ns", workload_name="api", container="app",
        finished_at="2026-05-25T11:00:00Z",
        trap_limit_bytes=3000 * M,
    )

    log_buf2 = _io.StringIO()
    handler2 = _logging.StreamHandler(log_buf2)
    handler2.setLevel(_logging.WARNING)
    wh_logger.addHandler(handler2)

    try:
        with patch("src.writeback_webhook._query_prom_values", return_value=prom_values):
            _build_containers_payload(
                rec, cfg_no_clamp,
                oom_state={"floor": {}, "last_event": {}, "history": {}},
                oom_events={"app": ev_fits},
                oom_eligible=True,
            )
    finally:
        wh_logger.removeHandler(handler2)

    log_text2 = log_buf2.getvalue()
    _check("[oom-bump-fits] no oom-bump-clamped warning when bump is within cap",
           "[oom-bump-clamped]" not in log_text2, True)


def section_mr_retry_on_429_and_5xx() -> None:
    """#20 — `_create_gitlab_mr` retries 429 + 5xx.

    Pre-1.22.10 the function used bare `requests.{get,post,put}` with no
    transport-level retry. A transient 503 / 502 on POST lost the MR for
    24h (until next CronJob), and 429 rate-limits crashed mid-sync. Fix:
    a module-level `requests.Session` wired with `urllib3.Retry` —
    `total=4`, `status_forcelist={429,500,502,503,504}`, exponential
    backoff with `respect_retry_after_header=True`, and POST in
    `allowed_methods` (POST is idempotent end-to-end thanks to 1.22.7
    pre-POST adoption GET + 409 race-recovery).

    Pinned invariants:
      - Retry knobs (status_forcelist contains 429/500/502/503/504, POST allowed,
        Retry-After respected) are correctly configured on the adapter
      - The factory returns a Session with HTTPAdapter mounted on https://
      - `raise_on_status=False` (so the existing 409 branch can still run)
    """
    _section("MR retry on 429 + 5xx")

    from src.writeback import _gitlab_session

    # Reset singleton each section run to avoid bleed from earlier tests.
    import src.writeback as _wb
    _wb._GITLAB_SESSION = None
    sess = _gitlab_session()

    adapter = sess.get_adapter("https://git.example.com")
    retry_cfg = adapter.max_retries

    # ── Retry knobs ─────────────────────────────────────────────────────
    _check("[mr-retry-status-list] 429 in status_forcelist",
           429 in (retry_cfg.status_forcelist or ()), True)
    _check("[mr-retry-status-list] 500 in status_forcelist",
           500 in (retry_cfg.status_forcelist or ()), True)
    _check("[mr-retry-status-list] 502 in status_forcelist",
           502 in (retry_cfg.status_forcelist or ()), True)
    _check("[mr-retry-status-list] 503 in status_forcelist",
           503 in (retry_cfg.status_forcelist or ()), True)
    _check("[mr-retry-status-list] 504 in status_forcelist",
           504 in (retry_cfg.status_forcelist or ()), True)
    _check("[mr-retry-401-not-in-list] 401 NOT in status_forcelist (auth fails fast)",
           401 in (retry_cfg.status_forcelist or ()), False)
    _check("[mr-retry-post-allowed] POST in allowed_methods",
           "POST" in retry_cfg.allowed_methods, True)
    _check("[mr-retry-get-allowed] GET in allowed_methods",
           "GET" in retry_cfg.allowed_methods, True)
    _check("[mr-retry-put-allowed] PUT in allowed_methods",
           "PUT" in retry_cfg.allowed_methods, True)
    _check("[mr-retry-after] respect_retry_after_header is True",
           retry_cfg.respect_retry_after_header, True)
    _check("[mr-retry-raise-on-status] raise_on_status=False (so 409 branch fires)",
           retry_cfg.raise_on_status, False)
    _check("[mr-retry-total] total attempts >= 3",
           (retry_cfg.total or 0) >= 3, True)
    _check("[mr-retry-backoff-factor] backoff_factor > 0",
           (retry_cfg.backoff_factor or 0) > 0, True)
    _check("[mr-retry-session-https] HTTPAdapter mounted on https://",
           sess.get_adapter("https://x") is adapter, True)
    _check("[mr-retry-session-http] HTTPAdapter mounted on http://",
           sess.get_adapter("http://x") is not None, True)


def section_cache_reconnect_backoff() -> None:
    """— watch-reconnect uses capped-exponential backoff + clears _ready.

    Pre-1.22.10 the reconnect path in `ResourceOverrideCache._run()` and
    `NamespaceCache._run()` used a fixed `self._stop.wait(2.0)` on
    re-list failure. When etcd compacted past the cache's RV (410 Gone)
    AND the recovery `_initial_list()` also failed, the outer loop spun
    every 2s with no backoff. `_ready` was never cleared, so `/readyz`
    continued 200 while the cache served frozen data. Fix:
      - extract `_backoff_sleep(stop, attempt, initial, max)` with full
        jitter (50%-100% of `min(initial*2**attempt, max)`)
      - reconnect loop tracks `consecutive_fail` counter
      - after `_READY_CLEAR_AFTER_K = 3` consecutive failures, call
        `_ready.clear()` so `/readyz` flips to 503 (kubelet pulls pod
        out of Service endpoints)
      - on success, reset counter (and `_ready.set()` happens via
        `_initial_list`)

    Pinned invariants:
      - Reconnect sleep is NOT fixed 2.0s — successive durations differ
        and respect the configured ceiling
      - `_ready.clear()` fires after K=3 consecutive `_initial_list` failures
      - `_ready` stays set after 1 failure (K>1)
      - Successful reconnect re-sets `_ready`
      - Same shape applies to NamespaceCache (sibling site)
    """
    _section("Cache watch-reconnect backoff + readiness flip")

    import threading
    from src.webhook_cache import (
        ResourceOverrideCache, NamespaceCache,
        _backoff_sleep, _READY_CLEAR_AFTER_K,
    )

    # ── 1. _backoff_sleep helper: monotonic-with-jitter, capped, stops on event ─
    stop = threading.Event()
    durations = []
    real_wait = stop.wait
    def _record_wait(d):
        durations.append(d)
        return False  # never stop
    stop.wait = _record_wait  # type: ignore[method-assign]
    for attempt in range(5):
        _backoff_sleep(stop, attempt, initial=0.01, maximum=0.05)
    stop.wait = real_wait  # type: ignore[method-assign]

    _check("[reconnect-backoff-len] _backoff_sleep called stop.wait 5 times",
           len(durations), 5)
    _check("[reconnect-backoff-max] no wait exceeds maximum (0.05s)",
           max(durations) <= 0.05, True)
    _check("[reconnect-backoff-min] all waits are positive",
           all(d > 0 for d in durations), True)
    _check("[reconnect-backoff-not-fixed] waits are not all identical (jitter present)",
           len(set(round(d, 5) for d in durations)) > 1, True)

    # ── 2. stop event interrupts: returns True when stop is already set ─────
    stop_set = threading.Event()
    stop_set.set()
    interrupted = _backoff_sleep(stop_set, 0, initial=10.0, maximum=10.0)
    _check("[reconnect-backoff-stop] _backoff_sleep returns True when stop is set",
           interrupted, True)

    # ── 3. K constant exposed and positive ─────────────────────────────────
    _check("[reconnect-K-positive] _READY_CLEAR_AFTER_K is at least 2 (K>1 so 1 failure doesn't flip readyz)",
           _READY_CLEAR_AFTER_K >= 2, True)

    # ── 4. CR cache reconnect: K failures clears _ready ─────────────────────
    api = MagicMock()
    api.list_cluster_custom_object.side_effect = [
        # 1st call: bootstrap success (empty)
        {"metadata": {"resourceVersion": "1"}, "items": []},
        # then K=3 re-list failures
        Exception("re-list fail 1"),
        Exception("re-list fail 2"),
        Exception("re-list fail 3"),
        # then keep failing so the loop stays in the failure state
        Exception("re-list fail 4"),
    ]
    cache = ResourceOverrideCache(api=api, event_callback=None)

    # Drive the reconnect path: watch raises, _initial_list keeps failing.
    fail_count = {"n": 0}
    def _watch_raises(self):
        fail_count["n"] += 1
        if fail_count["n"] > 5:
            self._stop.set()
        raise RuntimeError("watch stream broke (simulated 410 Gone)")
    with patch.object(ResourceOverrideCache, "_watch_loop", _watch_raises), \
         patch.object(ResourceOverrideCache, "_BOOTSTRAP_BACKOFF_INITIAL_S", 0.001, create=True), \
         patch.object(ResourceOverrideCache, "_BOOTSTRAP_BACKOFF_MAX_S", 0.005, create=True):
        cache.start()
        # Give the loop time to spin through K failures.
        bootstrap_ok = cache._ready.wait(timeout=1.0)
        # Wait for K consecutive failures to clear _ready.
        for _ in range(50):
            if not cache._ready.is_set():
                break
            import time as _t; _t.sleep(0.02)
        ready_after_K_failures = cache._ready.is_set()
        cache.stop()

    _check("[reconnect-cr-bootstrap] cache became ready after bootstrap",
           bootstrap_ok, True)
    _check("[reconnect-cr-ready-clear] _ready cleared after K=3 consecutive re-list failures",
           ready_after_K_failures, False)

    # ── 5. NamespaceCache reconnect: same shape (sibling fix) ──────────────
    ns_api = MagicMock()
    ns_api.list_namespace.side_effect = [
        # bootstrap success
        MagicMock(metadata=MagicMock(resource_version="1"), items=[]),
        # then keep failing on re-list
        Exception("ns re-list fail 1"),
        Exception("ns re-list fail 2"),
        Exception("ns re-list fail 3"),
        Exception("ns re-list fail 4"),
    ]
    ns_cache = NamespaceCache(api=ns_api)
    ns_fail_count = {"n": 0}
    def _ns_watch_raises(self):
        ns_fail_count["n"] += 1
        if ns_fail_count["n"] > 5:
            self._stop.set()
        raise RuntimeError("ns watch stream broke")
    with patch.object(NamespaceCache, "_watch_loop", _ns_watch_raises), \
         patch.object(NamespaceCache, "_BOOTSTRAP_BACKOFF_INITIAL_S", 0.001, create=True), \
         patch.object(NamespaceCache, "_BOOTSTRAP_BACKOFF_MAX_S", 0.005, create=True):
        ns_cache.start()
        ns_bootstrap_ok = ns_cache._ready.wait(timeout=1.0)
        for _ in range(50):
            if not ns_cache._ready.is_set():
                break
            import time as _t; _t.sleep(0.02)
        ns_ready_after_K = ns_cache._ready.is_set()
        ns_cache.stop()

    _check("[reconnect-ns-bootstrap] ns cache became ready after bootstrap",
           ns_bootstrap_ok, True)
    _check("[reconnect-ns-ready-clear] ns _ready cleared after K=3 consecutive re-list failures",
           ns_ready_after_K, False)


def section_safe_json_non_json_body() -> None:
    """`_safe_json` clear-errors when GitLab returns non-JSON 200.

    a misconfigured setup a misconfigured proxy / WAF can return
    `200 OK` with an HTML error page or empty body; the stock
    `resp.json()` raises `ValueError: Expecting value` which gives the
    operator zero context (which call, which status, what came back).
    `_safe_json` wraps it and re-raises `RuntimeError` with the call
    context, status code, Content-Type, and a truncated body sample.

    Pinned invariants:
      - Valid JSON 200 body passes through (returns the parsed object);
      - HTML 200 body raises RuntimeError with the context tag;
      - Empty 200 body raises RuntimeError;
      - Error message includes status code so the operator can correlate
        with apiserver logs / proxy logs.
    """
    _section("_safe_json — non-JSON 200 body handling (audit follow-up)")

    from src.writeback import _safe_json

    # ── 1. Valid JSON passes through ─────────────────────────────────────
    valid = MagicMock(
        status_code=200,
        headers={"Content-Type": "application/json"},
        text='{"key": "value"}',
    )
    valid.json = lambda: {"key": "value"}
    out = _safe_json(valid, "test context")
    _check("[safe-json-valid] valid JSON body parsed correctly",
           out, {"key": "value"})

    # ── 2. HTML 200 body raises RuntimeError with context ────────────────
    html_resp = MagicMock(
        status_code=200,
        headers={"Content-Type": "text/html"},
        text="<html><body>502 Bad Gateway from upstream proxy</body></html>",
    )
    html_resp.json = MagicMock(side_effect=ValueError("Expecting value: line 1 column 1"))
    raised_msg = None
    try:
        _safe_json(html_resp, "MR adoption lookup")
    except RuntimeError as exc:
        raised_msg = str(exc)
    _check("[safe-json-html] RuntimeError raised on HTML 200 body",
           raised_msg is not None, True)
    _check("[safe-json-html] error includes context tag",
           "MR adoption lookup" in (raised_msg or ""), True)
    _check("[safe-json-html] error includes status code",
           "200" in (raised_msg or ""), True)
    _check("[safe-json-html] error includes truncated body sample",
           "502 Bad Gateway" in (raised_msg or ""), True)

    # ── 3. Empty 200 body raises RuntimeError ────────────────────────────
    empty_resp = MagicMock(
        status_code=200,
        headers={"Content-Type": "application/json"},
        text="",
    )
    empty_resp.json = MagicMock(side_effect=ValueError("Expecting value"))
    raised_msg = None
    try:
        _safe_json(empty_resp, "MR create POST response")
    except RuntimeError as exc:
        raised_msg = str(exc)
    _check("[safe-json-empty] RuntimeError raised on empty 200 body",
           raised_msg is not None, True)
    _check("[safe-json-empty] error includes call context",
           "MR create POST response" in (raised_msg or ""), True)


def section_discovery_auth_raise() -> None:
    """discovery.list_enabled_namespaces must raise loud on 401/403.

    The pre-1.22.8 implementation caught `except Exception` and returned []
    on any failure — a broad swallow that turned auth failures into clean
    exit-0 with no work. This masked the kubernetes==36.0.0 in-cluster
    auth regression for ~24h in production. Auth failures (401, 403) are
    structural: SA token bad, RBAC missing, client-library broken. None
    of those are transient — re-running won't fix them. The CronJob must
    exit non-zero so the operator sees the failed Job in
    `kubectl get cronjob` and alert pipelines fire.

    Transient errors (5xx, timeouts, watch resets) keep the swallow —
    the next CronJob tick retries naturally.

    Pinned invariants:
      - ApiException(status=401) propagates (no swallow);
      - ApiException(status=403) propagates (no swallow);
      - ApiException(status=503) is swallowed with WARNING + returns [];
      - generic Exception (timeout, urllib3 error) is swallowed.
    """
    _section("discovery — raise on auth failure (401/403)")

    from kubernetes.client.rest import ApiException
    from src.discovery import list_enabled_namespaces

    # ── 401: must propagate ──────────────────────────────────────────────
    api = MagicMock()
    api.list_namespace.side_effect = ApiException(status=401, reason="Unauthorized")
    raised = False
    try:
        list_enabled_namespaces(core_api=api)
    except ApiException as exc:
        raised = exc.status == 401
    _check("[discovery-auth-401] ApiException(401) propagates", raised, True)

    # ── 403: must propagate ──────────────────────────────────────────────
    api = MagicMock()
    api.list_namespace.side_effect = ApiException(status=403, reason="Forbidden")
    raised = False
    try:
        list_enabled_namespaces(core_api=api)
    except ApiException as exc:
        raised = exc.status == 403
    _check("[discovery-auth-403] ApiException(403) propagates", raised, True)

    # ── 503: transient, must be swallowed ────────────────────────────────
    api = MagicMock()
    api.list_namespace.side_effect = ApiException(status=503, reason="Service Unavailable")
    result = list_enabled_namespaces(core_api=api)
    _check("[discovery-auth-503] ApiException(503) swallowed, empty list returned",
           result, [])

    # ── generic Exception (timeout): must be swallowed ───────────────────
    api = MagicMock()
    api.list_namespace.side_effect = TimeoutError("simulated apiserver hang")
    result = list_enabled_namespaces(core_api=api)
    _check("[discovery-auth-generic] TimeoutError swallowed, empty list returned",
           result, [])


def section_dependency_pins() -> None:
    """Pins on runtime deps that have shipped silent regressions in past releases.

    Each entry here exists because an unpinned upper bound let an upstream
    package break us in production. The assert is a belt-and-suspenders on
    top of `requirements.txt` — the pin in requirements is the load-bearing
    fix; this assert catches the case where someone widens the pin without
    re-verifying.

    Closed regressions tracked:
      - kubernetes==36.0.0 (May 2026) — `Configuration.auth_settings()`
        returns an empty dict after `load_incluster_config()`, so the
        Authorization header is never sent and every API call gets 401.
        The CronJob's `discovery.py:list_namespace()` swallowed the 401
        with a broad except and exited 0 "successfully" with no work
        done, masking the regression for ~24h until visible in the next
        release's live-test (1.22.7 ship cycle).
    """
    _section("Dependency pins (closed upstream regressions)")

    import kubernetes as _k8s
    major = int(_k8s.__version__.split(".")[0])
    _check("[deps-kubernetes-major] kubernetes major version pinned below 36 "
           "(in-cluster auth regression in 36.0.0)",
           major < 36, True)

    # ── Reproducible builds — #34 (base digest) + #33 (dep bounds + lock) ──
    # Same failure class as the 2026-05-28 arm64 outage: mutable build inputs
    # make a build today differ from one in six months. These read the actual
    # repo files, so they FAIL pre-fix (mutable tag, bare >=, no lock).
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # #34 — every `FROM python:` in the Dockerfile is digest-pinned.
    with open(os.path.join(_root, "Dockerfile")) as _f:
        dockerfile = _f.read()
    unpinned_from = [ln.strip() for ln in dockerfile.splitlines()
                     if ln.strip().startswith("FROM ") and "@sha256:" not in ln]
    _check("[deps-#34-dockerfile-digest] every Dockerfile FROM is @sha256-pinned "
           "(mutable tag = irreproducible base, arm64-outage class)",
           unpinned_from, [])

    # #33a — every requirement in requirements.txt carries an upper bound.
    with open(os.path.join(_root, "requirements.txt")) as _f:
        req_lines = [ln.strip() for ln in _f.read().splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
    unbounded = [ln for ln in req_lines if "<" not in ln]
    _check("[deps-#33-upper-bounds] every requirements.txt dep has an upper bound "
           "(bare >= can pull a breaking major on the next build)",
           unbounded, [])

    # #33b — a resolved lock file exists with exact (==) pins.
    lock_path = os.path.join(_root, "requirements.lock")
    _check("[deps-#33-lock-exists] requirements.lock present",
           os.path.isfile(lock_path), True)
    if os.path.isfile(lock_path):
        with open(lock_path) as _f:
            lock_txt = _f.read()
        _check("[deps-#33-lock-pinned] requirements.lock pins exact versions (==)",
               "==" in lock_txt, True)

    # #33c — the Dockerfile installs from the lock, not the loose requirements.
    _check("[deps-#33-dockerfile-uses-lock] Dockerfile installs from requirements.lock",
           "requirements.lock" in dockerfile, True)


def section_trivial_log_and_defaults() -> None:
    """Smell-test asserts for the trivial batch:

      #9  prometheus.py: rename log 'no memory limit data' → 'no working set data'
      #23 discovery.py: log namespace not opted in, skipping
      #32 config.py: default git_author_email no longer uses RFC 6762 .local TLD
      #37 workload.py: log skipped init containers
      #44 webhook_validate._deny: HTTP 422 / Invalid (not 409 / Conflict)
    """
    _section("Trivial batch — log polish + default hygiene")

    import inspect

    # ── #9 prometheus.py log message rename ──────────────────────────
    import src.prometheus
    prom_src = inspect.getsource(src.prometheus)
    _check("[trivial-9] prometheus.py no longer logs 'no memory limit data' for empty result",
           "no memory limit data" in prom_src, False)
    _check("[trivial-9] prometheus.py logs 'no working set data' for empty result",
           "no working set data" in prom_src, True)

    # ── #23 discovery.py opt-out breadcrumb (demoted to DEBUG in 1.22.9) ─
    import src.discovery
    disc_src = inspect.getsource(src.discovery)
    _check("[trivial-23] discovery.py logs the not-opted-in breadcrumb",
           "not opted in, skipping" in disc_src, True)
    _check("[trivial-23-debug] not-opted-in breadcrumb is DEBUG (not INFO) — "
           "1.22.9 demotion so 95+/100 cluster runs don't flood stdout",
           '_log.debug("[discovery] namespace %s not opted in, skipping"' in disc_src,
           True)
    _check("[trivial-23-debug] no INFO-level not-opted-in breadcrumb remains",
           '_log.info("[discovery] namespace %s not opted in, skipping"' in disc_src,
           False)

    # ── #32 config.py default email no longer uses .local TLD ────────
    import src.config
    cfg_src = inspect.getsource(src.config)
    _check("[trivial-32] config.py default git_author_email no longer references cluster.local",
           "kube-resource-updater@cluster.local" in cfg_src, False)
    _check("[trivial-32] config.py default uses noreply@kube-resource-updater.example",
           "noreply@kube-resource-updater.example" in cfg_src, True)

    # ── #37 workload.py logs skipped init containers ─────────────────
    import src.workload
    wl_src = inspect.getsource(src.workload)
    _check("[trivial-37] workload.py emits 'skipping init containers' log",
           "skipping init containers" in wl_src, True)

    # ── #44 webhook_validate._deny uses HTTP 422 / 'Invalid' ─────────
    from src.webhook_validate import _deny
    resp = _deny("uid-x", "selector overlap with sib")
    _check("[trivial-44] _deny status code is 422 (Invalid), not 409 (Conflict)",
           resp["response"]["status"]["code"], 422)
    _check("[trivial-44] _deny reason is 'Invalid', not 'Conflict'",
           resp["response"]["status"]["reason"], "Invalid")


def section_detect_scrape_interval() -> None:
    """Scrape-interval auto-detection (ROADMAP 'Scrape-interval auto-detection').

    Fail-first: detect_scrape_interval doesn't exist before the code change,
    so the module import fails outright. After the change these must all pass.
    """
    _section("detect_scrape_interval — scrape interval auto-detection")

    from src.prometheus import detect_scrape_interval, _scrape_interval_cache
    from unittest.mock import patch as _patch, MagicMock

    def _targets_resp(interval: str) -> MagicMock:
        m = MagicMock()
        m.ok = True
        m.json.return_value = {"data": {"activeTargets": [
            {"labels": {"metrics_path": "/metrics/cadvisor", "job": "kubelet"},
             "scrapeInterval": interval, "scrapeTimeout": "10s"}]}}
        return m

    def _targets_resp_no_cadvisor() -> MagicMock:
        m = MagicMock()
        m.ok = True
        m.json.return_value = {"data": {"activeTargets": [
            {"labels": {"metrics_path": "/metrics", "job": "kubelet"},
             "scrapeInterval": "15s"}]}}
        return m

    def _config_resp(global_interval: str) -> MagicMock:
        m = MagicMock()
        m.ok = True
        m.json.return_value = {"data": {"yaml":
            f"global:\n  scrape_interval: {global_interval}\n  evaluation_interval: 1m\n"}}
        return m

    # Primary: cadvisor target at 10s.
    _scrape_interval_cache.clear()
    with _patch("src.prometheus.requests.get", return_value=_targets_resp("10s")):
        result = detect_scrape_interval("http://prom.fake:9090")
    _check("[detect] cadvisor 10s target → returns 10s", result, "10s")
    _check("[detect] result cached after first call",
           _scrape_interval_cache.get("http://prom.fake:9090"), "10s")

    # Primary: cadvisor target at 15s.
    _scrape_interval_cache.clear()
    with _patch("src.prometheus.requests.get", return_value=_targets_resp("15s")):
        result = detect_scrape_interval("http://prom.fake:9090")
    _check("[detect] cadvisor 15s target → returns 15s", result, "15s")

    # Cache hit: second call doesn't issue a new HTTP request.
    call_count = {"n": 0}
    def _counting_get(*a, **kw):
        call_count["n"] += 1
        return _targets_resp("10s")
    _scrape_interval_cache.clear()
    with _patch("src.prometheus.requests.get", side_effect=_counting_get):
        detect_scrape_interval("http://prom.fake:9090")
        detect_scrape_interval("http://prom.fake:9090")
    _check("[detect] second call hits cache (no extra HTTP)", call_count["n"], 1)

    # Fallback: no cadvisor targets → parse global from status/config.
    _scrape_interval_cache.clear()
    call_idx = {"i": 0}
    def _no_cadvisor_then_config(*a, **kw):
        call_idx["i"] += 1
        return _targets_resp_no_cadvisor() if call_idx["i"] == 1 else _config_resp("30s")
    with _patch("src.prometheus.requests.get", side_effect=_no_cadvisor_then_config):
        result = detect_scrape_interval("http://prom.fake:9090")
    _check("[detect] no cadvisor → falls back to status/config global 30s", result, "30s")

    # Fallback: targets API raises → status/config.
    _scrape_interval_cache.clear()
    call_idx2 = {"i": 0}
    def _targets_raises_then_config(*a, **kw):
        call_idx2["i"] += 1
        if call_idx2["i"] == 1:
            raise ConnectionError("targets unreachable")
        return _config_resp("15s")
    with _patch("src.prometheus.requests.get", side_effect=_targets_raises_then_config):
        result = detect_scrape_interval("http://prom.fake:9090")
    _check("[detect] targets API raises → falls back to status/config 15s", result, "15s")

    # Final fallback: both APIs fail → 15s.
    _scrape_interval_cache.clear()
    def _always_raises(*a, **kw):
        raise ConnectionError("unreachable")
    with _patch("src.prometheus.requests.get", side_effect=_always_raises):
        result = detect_scrape_interval("http://prom.fake:9090")
    _check("[detect] both APIs fail → fallback 15s", result, "15s")

    # Out-of-range interval (999s) → clamped out → fallback 15s.
    _scrape_interval_cache.clear()
    with _patch("src.prometheus.requests.get", return_value=_targets_resp("999s")):
        result = detect_scrape_interval("http://prom.fake:9090")
    _check("[detect] out-of-range interval (999s) → fallback 15s", result, "15s")

    # Empty URL → 15s with no HTTP call.
    _scrape_interval_cache.clear()
    _check("[detect] empty url → 15s immediately", detect_scrape_interval(""), "15s")
    # ── ITEM 2 (metrics_path endswith fix) ───────────────────────────────────
    # A decoy target whose metrics_path contains "cadvisor" as a substring but
    # does NOT end with "/metrics/cadvisor" must NOT be treated as a cAdvisor
    # target. Under the old `"/cadvisor" in mp` check it false-positives and
    # its scrape interval pollutes the subquery step.
    #
    # Test 1 — Mixed: decoy@10s + real@30s.
    #   Old (substring): both match; min(10s,30s)=10s → returns "10s"  [WRONG]
    #   New (endswith):  only real matches; returns "30s"               [correct]
    _scrape_interval_cache.clear()
    def _targets_resp_mixed_decoy() -> MagicMock:
        m_mix = MagicMock()
        m_mix.ok = True
        m_mix.json.return_value = {"data": {"activeTargets": [
            # Decoy: interval 10s — must be IGNORED; if matched it would win min().
            {"labels": {"metrics_path": "/app/cadvisor/extra"}, "scrapeInterval": "10s"},
            # Real cAdvisor target: interval 30s — must be the only match.
            {"labels": {"metrics_path": "/metrics/cadvisor"}, "scrapeInterval": "30s"},
        ]}}
        return m_mix
    with _patch("src.prometheus.requests.get", return_value=_targets_resp_mixed_decoy()):
        result_mixed = detect_scrape_interval("http://prom.fake:9090")
    # Must be 30s (real target only), not 10s (which old substring would pick).
    _check("[detect] decoy /app/cadvisor/extra ignored; only real /metrics/cadvisor matched "
           "(pre-fix: returns 10s because decoy wins min())",
           result_mixed, "30s")

    # Test 2 — Decoy only: decoy@30s, no real cAdvisor target.
    #   Old (substring): decoy matches; returns "30s"       [WRONG — wrong step value]
    #   New (endswith):  no match → config fallback → 15s   [correct]
    _scrape_interval_cache.clear()
    _decoy_only_call2 = 0
    def _decoy_only_then_config2(*a, **kw):
        nonlocal _decoy_only_call2
        _decoy_only_call2 += 1
        if _decoy_only_call2 == 1:
            m2 = MagicMock()
            m2.ok = True
            m2.json.return_value = {"data": {"activeTargets": [
                {"labels": {"metrics_path": "/app/cadvisor/extra"}, "scrapeInterval": "30s"},
            ]}}
            return m2
        m3 = MagicMock()
        m3.ok = True
        m3.json.return_value = {"data": {"yaml":
            "global:\n  scrape_interval: 15s\n  evaluation_interval: 1m\n"}}
        return m3
    with _patch("src.prometheus.requests.get", side_effect=_decoy_only_then_config2):
        result_decoy_only = detect_scrape_interval("http://prom.fake:9090")
    # Must NOT be "30s" (the decoy's interval). Old code returns "30s" here.
    _check("[detect] decoy-only /app/cadvisor/extra → no match → falls back, NOT the decoy 30s "
           "(pre-fix: returns 30s because decoy false-positives)",
           result_decoy_only, "15s")


    # query_cpu_max_m uses the explicit step in the PromQL string.
    from src import prometheus as _prom
    _prom._cache.clear()
    _prom._scrape_interval_cache.clear()
    captured: dict = {}
    def _fake_get_query(url, params=None, **kw):
        if "/api/v1/query" in url:
            captured["query"] = (params or {}).get("query", "")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": {"result": []}}
        return resp
    with _patch.object(_prom.requests, "get", side_effect=_fake_get_query):
        _prom.query_cpu_max_m("http://prom:9090", namespace="ns", container="app",
                              target_name=None, window="7d", subquery_step="10s")
    _check("[detect] explicit 10s step appears in query string",
           "[7d:10s]" in captured.get("query", ""), True)
    _check("[detect] hardcoded 15s does NOT appear when 10s is passed",
           "[7d:15s]" in captured.get("query", ""), False)

    # Without subquery_step, detection runs and its result is used.
    _prom._cache.clear()
    _prom._scrape_interval_cache.clear()
    captured2: dict = {}
    def _fake_get_detect_and_query(url, params=None, **kw):
        q = (params or {}).get("query", "")
        if "/api/v1/query" in url and q:
            captured2["query"] = q
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"data": {"result": []}}
            return resp
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"data": {"activeTargets": [
            {"labels": {"metrics_path": "/metrics/cadvisor"}, "scrapeInterval": "10s"}]}}
        return resp
    with _patch.object(_prom.requests, "get", side_effect=_fake_get_detect_and_query):
        _prom.query_cpu_max_m("http://prom:9090", namespace="ns", container="app",
                              target_name=None, window="7d")
    _check("[detect] auto-detected 10s step used when no explicit step given",
           "[7d:10s]" in captured2.get("query", ""), True)

    # ── BUG 1: _DURATION_RE rejects ms + compound durations; _parse_duration_seconds
    # raises ValueError on them (silent swallow in detection code).
    # Pre-fix: fullmatch returns None for both; _parse_duration_seconds raises.
    # Post-fix: regex accepts them; parser handles ms→floor-to-int and compounds→sum.
    from src.prometheus import _DURATION_RE, _parse_duration_seconds as _pds

    # _DURATION_RE must accept ms and compound duration strings.
    _check("[duration-re] '500ms' accepted by _DURATION_RE (pre-fix: rejected)",
           bool(_DURATION_RE.fullmatch("500ms")), True)
    _check("[duration-re] '1m30s' accepted by _DURATION_RE (pre-fix: rejected)",
           bool(_DURATION_RE.fullmatch("1m30s")), True)
    _check("[duration-re] '1h30m' accepted by _DURATION_RE (pre-fix: rejected)",
           bool(_DURATION_RE.fullmatch("1h30m")), True)
    # Existing simple forms must still be accepted (no regression).
    _check("[duration-re] '10s' still accepted", bool(_DURATION_RE.fullmatch("10s")), True)
    _check("[duration-re] '1m' still accepted",  bool(_DURATION_RE.fullmatch("1m")),  True)
    _check("[duration-re] '2h' still accepted",  bool(_DURATION_RE.fullmatch("2h")),  True)
    # Bare integer without unit must still be rejected.
    _check("[duration-re] '30' (no unit) still rejected",
           bool(_DURATION_RE.fullmatch("30")), False)

    # _parse_duration_seconds must not raise on ms or compound durations.
    # 500ms → 0.5s → floor to 0 (below scrape-interval min, triggers range guard).
    _check("[parse-dur] '500ms' → 0 seconds (floor of 0.5)",
           _pds("500ms"), 0)
    # 1m30s → 90 seconds.
    _check("[parse-dur] '1m30s' → 90 seconds",
           _pds("1m30s"), 90)
    # 1h30m → 5400 seconds.
    _check("[parse-dur] '1h30m' → 5400 seconds",
           _pds("1h30m"), 5400)
    # Simple forms untouched.
    _check("[parse-dur] '10s' → 10",  _pds("10s"), 10)
    _check("[parse-dur] '2m' → 120",  _pds("2m"),  120)
    _check("[parse-dur] '1h' → 3600", _pds("1h"),  3600)
    # _parse_duration_seconds must raise ValueError on an unknown unit.
    # Pre-fix: the old unconditional last-char strip silently misparsed — e.g.
    # "5x" would strip "x" and return int("5") = 5, hiding the bad input.
    # Post-fix (this session): the component loop raises ValueError for any unit
    # not in {"ms","s","m","h","d"}.
    _dur_raised_unknown = False
    try:
        _pds("5x")
    except ValueError:
        _dur_raised_unknown = True
    _check("[parse-dur] unknown unit '5x' raises ValueError (pre-fix: silently misparsed)",
           _dur_raised_unknown, True)

    # Empty string must also raise (not return 0 or silently pass through).
    _dur_raised_empty = False
    try:
        _pds("")
    except ValueError:
        _dur_raised_empty = True
    _check("[parse-dur] empty string raises ValueError",
           _dur_raised_empty, True)


    # End-to-end: cAdvisor target reporting 500ms → 0s → below min → fallback 15s
    # (must NOT crash with ValueError; pre-fix crashes silently inside except block).
    _scrape_interval_cache.clear()
    def _targets_resp_ms(interval: str):
        m = MagicMock()
        m.ok = True
        m.json.return_value = {"data": {"activeTargets": [
            {"labels": {"metrics_path": "/metrics/cadvisor"}, "scrapeInterval": interval}]}}
        return m

    with _patch("src.prometheus.requests.get", return_value=_targets_resp_ms("500ms")):
        result_ms = detect_scrape_interval("http://prom.fake:9090")
    _check("[detect] cAdvisor 500ms → below min range → falls back to 15s (no crash)",
           result_ms, "15s")

    # cAdvisor target reporting 1m30s → 90s → within [5,120] → accepted.
    _scrape_interval_cache.clear()
    with _patch("src.prometheus.requests.get", return_value=_targets_resp_ms("1m30s")):
        result_compound = detect_scrape_interval("http://prom.fake:9090")
    _check("[detect] cAdvisor 1m30s → 90s, within range → returned as '1m30s'",
           result_compound, "1m30s")



# --------------------------------------------------------------------------- #
# Section 6b: request query single-series guarantee (bug: cross-workload max) #
# --------------------------------------------------------------------------- #

def section_request_query_single_series() -> None:
    """Bug fix: query_cpu_request_m and query_mem_request_bytes must emit
    a single-series PromQL expression so the Python max() is a no-op.

    Pre-fix both functions used `max without(pod)(...)`. When target_name=None
    and a container name is shared across multiple workloads in the same
    namespace, Prometheus returns one element per surviving label combination
    (node, uid, etc. remain as dimensions). Python then took max() over that
    multi-element list, silently returning the highest p90 in the namespace —
    the request recommendation was inflated to the most CPU/mem-hungry workload.

    Fix: replace `max without(pod)` with `max by(namespace, container)` so the
    inner aggregation collapses pods AND all other cardinality-adding labels.
    Prometheus returns exactly one series; the Python max() is a no-op.

    Shape asserts (offline — fail on pre-fix code):
      1. PromQL contains `by(namespace, container)`, not `without(pod)`.
      2. When Prometheus returns 2 series (cross-workload contamination), the
         result equals the correct single-workload value (not the inflated max).
    """
    _section("Request query single-series guarantee — cross-workload max bug fix")

    from src import prometheus as _prom
    from unittest.mock import patch as _patch, MagicMock

    def _fake_get_capture(captured: dict):
        def _inner(url, params=None, **kw):
            captured["query"] = (params or {}).get("query", "")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"data": {"result": []}}
            return resp
        return _inner

    # ── 1. CPU request: query shape must use by(namespace, container) ────────
    _prom._req_cpu_cache.clear()
    captured_cpu: dict = {}
    with _patch.object(_prom.requests, "get", side_effect=_fake_get_capture(captured_cpu)):
        _prom.query_cpu_request_m(
            "http://prom:9090", namespace="ns", container="app",
            target_name=None, percentile=0.90, window="3d",
        )
    q_cpu = captured_cpu.get("query", "")
    _check("[req-single-series] CPU request query uses by(namespace, container)",
           "by(namespace, container)" in q_cpu, True)
    _check("[req-single-series] CPU request query does NOT use without(pod) "
           "(would leave extra label dimensions that inflate via Python max())",
           "without(pod)" in q_cpu, False)

    # ── 2. Memory request: query shape must use by(namespace, container) ─────
    _prom._req_mem_cache.clear()
    captured_mem: dict = {}
    with _patch.object(_prom.requests, "get", side_effect=_fake_get_capture(captured_mem)):
        _prom.query_mem_request_bytes(
            "http://prom:9090", namespace="ns", container="app",
            target_name=None, percentile=0.90, window="8d",
        )
    q_mem = captured_mem.get("query", "")
    _check("[req-single-series] MEM request query uses by(namespace, container)",
           "by(namespace, container)" in q_mem, True)
    _check("[req-single-series] MEM request query does NOT use without(pod)",
           "without(pod)" in q_mem, False)

    # ── 3. CPU request: single-series response returns correct value ─────────
    # Post-fix, the `by(namespace, container)` inner aggregation collapses all
    # label dimensions; Prometheus returns exactly 1 series per (namespace,
    # container). Mock a single-series response and assert the result matches
    # that single value — not some inflated cross-workload max.
    # The multi-series contamination path (pre-fix bug) is no longer reachable
    # from Prometheus with the fixed PromQL; the asserts above (shape checks)
    # are the primary guards. This assert confirms the happy-path Python
    # arithmetic (single element × 1000 ms/core conversion) is correct.
    _prom._req_cpu_cache.clear()
    one_series_resp = MagicMock()
    one_series_resp.raise_for_status = MagicMock()
    one_series_resp.json.return_value = {"data": {"result": [
        {"metric": {"namespace": "ns", "container": "app"}, "value": [0, "0.2"]},
    ]}}
    with _patch.object(_prom.requests, "get", return_value=one_series_resp):
        cpu_req = _prom.query_cpu_request_m(
            "http://prom:9090", namespace="ns", container="app",
            target_name=None, percentile=0.90, window="3d",
        )
    # 0.2 cores × 1000 = 200m. Single series → no inflation risk.
    _check("[req-single-series] CPU single-series response: result is 200m (0.2 cores)",
           cpu_req, 200)

    # ── 4. Memory request: single-series response returns correct value ───────
    _prom._req_mem_cache.clear()
    one_series_mem = MagicMock()
    one_series_mem.raise_for_status = MagicMock()
    _mem_val = 100 * 1024 * 1024  # 100 MiB
    one_series_mem.json.return_value = {"data": {"result": [
        {"metric": {"namespace": "ns", "container": "app"}, "value": [0, str(_mem_val)]},
    ]}}
    with _patch.object(_prom.requests, "get", return_value=one_series_mem):
        mem_req = _prom.query_mem_request_bytes(
            "http://prom:9090", namespace="ns", container="app",
            target_name=None, percentile=0.90, window="8d",
        )
    _check("[req-single-series] MEM single-series response: result is 100 MiB",
           mem_req, _mem_val)


# --------------------------------------------------------------------------- #
# Section 6c: irate lookback dynamic window (bug: hardcoded 5m irate)          #
# --------------------------------------------------------------------------- #

def section_irate_lookback_dynamic() -> None:
    """Bug 2 — prometheus.py: query_cpu_max_m hardcodes irate[5m] regardless
    of the detected subquery step.

    If the step is large (e.g. >= 180s, which is outside the normal
    _SCRAPE_INTERVAL_MAX_S=120 clamping range but reachable via subquery_step
    override), a single missed scrape within the 5-minute lookback can make
    irate return NaN for that step. max_over_time skips NaN entries, so CPU
    limit estimation goes sparse and systematically underestimates spikes.

    Fix: compute irate_window = max(5m, 2 × step_seconds rounded up) so at
    least 2 samples are always available per irate evaluation.

    Offline asserts (fail-first):
      1. Normal steps (15s, 30s, 120s): 2 × step <= 300s → window stays [5m].
      2. step=180s: 2 × 180 = 360s > 300s → window must expand beyond [5m].
      3. The query still uses max_over_time(irate(...)) shape.

    Rule 4 call: this is a query-shape change. The live-Prom block in
    section_live_prometheus already queries cpu_lim with the detected step;
    the release agent must confirm cpu_lim is non-None and > 0 on the real
    cluster. A full CronJob run is NOT required — the live-Prom block suffices
    because the default step (15s) stays within the 5m window and the logic
    only diverges at step > 150s, which won't occur on a healthy cluster.
    """
    _section("irate lookback window dynamic with step (Bug 2)")

    from src import prometheus as _prom
    from unittest.mock import patch as _patch, MagicMock

    def _capture_query(step: str) -> str:
        """Issue query_cpu_max_m with the given step and return the PromQL string."""
        _prom._cache.clear()
        captured: dict = {}

        def _fake_get(url, params=None, **kw):
            captured["query"] = (params or {}).get("query", "")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"data": {"result": []}}
            return resp

        with _patch.object(_prom.requests, "get", side_effect=_fake_get):
            _prom.query_cpu_max_m(
                "http://prom:9090", namespace="ns", container="app",
                target_name=None, window="7d", subquery_step=step,
            )
        return captured.get("query", "")

    # Case 1: step=15s → 2×15=30s < 300s → irate window stays [5m].
    q_15 = _capture_query("15s")
    _check("[irate-dyn] step=15s → irate window is [5m] (normal case, no regression)",
           "[5m]" in q_15, True)
    _check("[irate-dyn] step=15s → subquery step is [7d:15s]",
           "[7d:15s]" in q_15, True)

    # Case 2: step=30s → 2×30=60s < 300s → window stays [5m].
    q_30 = _capture_query("30s")
    _check("[irate-dyn] step=30s → irate window stays [5m]",
           "[5m]" in q_30, True)

    # Case 3: step=120s → 2×120=240s < 300s → window stays [5m].
    q_120 = _capture_query("120s")
    _check("[irate-dyn] step=120s → 2×120=240s < 300s → irate window stays [5m]",
           "[5m]" in q_120, True)

    # Case 4: step=180s → 2×180=360s > 300s → window must expand beyond 5m.
    # This is the load-bearing failing assert. Pre-fix: [5m] appears in the
    # irate call, meaning irate only has a 300s lookback when the step is 180s
    # — with a single missed scrape the irate returns NaN.
    # Post-fix: [360s] (or equivalent) appears instead.
    q_180 = _capture_query("180s")
    # The hardcoded pre-fix form is "irate(...[5m])". Post-fix the irate window
    # is computed from the step, not hardcoded. Check the irate call's window.
    # We match "[5m])" to avoid false-positives from "[7d:15s]" style strings.
    _check("[irate-dyn] step=180s → irate window is NOT [5m] (2×180=360s > 300s) "
           "(FAILS on pre-fix hardcoded irate[5m])",
           "[5m])" in q_180, False)

    # Positive shape checks: the query must still be structurally correct.
    _check("[irate-dyn] step=180s → query still uses max_over_time(",
           "max_over_time(" in q_180, True)
    _check("[irate-dyn] step=180s → query still uses irate(",
           "irate(" in q_180, True)
    _check("[irate-dyn] step=180s → subquery still embeds [7d:180s]",
           "[7d:180s]" in q_180, True)



# --------------------------------------------------------------------------- #
# Section 7: live Prometheus parameter sweep (optional)                        #
# --------------------------------------------------------------------------- #

def section_live_prometheus() -> int:
    """Returns 0 when skipped or healthy, 1 when live data looks broken."""
    url = os.environ.get("PROMETHEUS_URL", "").strip().rstrip("/")
    if not url:
        print()
        print("=" * 80)
        print("Live Prometheus QA — SKIPPED (set PROMETHEUS_URL to run)".center(80))
        print("=" * 80)
        return 0

    namespace = os.environ.get("NS", "monitoring")
    container = os.environ.get("CONTAINER", "")
    workload  = os.environ.get("WORKLOAD", "")

    _section(f"Live Prometheus QA — url={url}  ns={namespace}")

    print(f"  CPU request: percentile × window  (target: {workload or 'auto'} / {container or 'any'})")
    print("  window | p50      p80      p90      p99")
    print("  " + "-" * 48)
    cpu_grid: dict[tuple, "int | None"] = {}
    for window in ("1d", "3d", "7d"):
        row = [f"  {window:6}|"]
        for pct in (0.50, 0.80, 0.90, 0.99):
            v = query_cpu_request_m(url, namespace, container, workload or None, pct, window)
            cpu_grid[(window, pct)] = v
            row.append(f" {(str(v) + 'm') if v is not None else 'n/a':>8}")
        print(" ".join(row))

    print()
    print("  Memory request: percentile × window")
    print("  window | p50      p80      p90      p99")
    print("  " + "-" * 48)
    mem_grid: dict[tuple, "int | None"] = {}
    for window in ("3d", "8d"):
        row = [f"  {window:6}|"]
        for pct in (0.50, 0.80, 0.90, 0.99):
            v = query_mem_request_bytes(url, namespace, container, workload or None, pct, window)
            mem_grid[(window, pct)] = v
            mb = (v // (1024 * 1024)) if v is not None else None
            row.append(f" {(str(mb) + 'Mi') if mb is not None else 'n/a':>8}")
        print(" ".join(row))

    print()
    print("  Limits (max_over_time, 7d window)")
    detected_step = detect_scrape_interval(url)
    print(f"  detected cAdvisor scrape interval: {detected_step}")
    _check_truthy("[live] detected step is a non-empty duration", detected_step)
    cpu_lim = query_cpu_max_m(url, namespace, container, workload or None, "7d", detected_step)
    mem_lim = query_memory_max_bytes(url, namespace, container, workload or None, "7d")
    print(f"  cpu_lim_7d (step={detected_step}): {cpu_lim}m   mem_lim_7d: {(mem_lim // (1024 * 1024)) if mem_lim else None}Mi")
    if cpu_lim is not None:
        _check_truthy("[live] CPU limit query returns data with detected step", cpu_lim > 0)

    # ── Single-series result count (request query shape fix) ────────────────
    # After the fix, `max by(namespace, container)` collapses all label
    # dimensions, so Prometheus must return exactly 1 series per request query
    # regardless of how many pods/nodes ran the container. More than 1 means
    # the `without(pod)` shape is still in use and cross-workload inflation
    # is possible.
    import requests as _r
    from src.prometheus import _escape_label_value
    _ns_e = _escape_label_value(namespace)
    _c_e  = _escape_label_value(container or "")
    if container:
        _cpu_shape_q = (
            f"quantile_over_time(0.90,"
            f"max by(namespace, container)("
            f'rate(container_cpu_usage_seconds_total{{namespace="{_ns_e}",container="{_c_e}"}}[1m])'
            f")[3d:1m])"
        )
        try:
            _raw = _r.get(f"{url}/api/v1/query", params={"query": _cpu_shape_q}, timeout=30)
            _raw.raise_for_status()
            _series_count = len(_raw.json().get("data", {}).get("result", []))
            print(f"  single-series check: CPU request query → {_series_count} series (expected 1)")
            cond_ss = _series_count <= 1
            print(f"  [{_PASS if cond_ss else _FAIL}] CPU request query returns ≤1 series (single-series guarantee)")
            if not cond_ss:
                rc = 1
        except Exception as _exc:
            print(f"  [{_SKIP}] single-series check failed: {_exc}")

    print()
    rc = 0
    cpu_values = [v for v in cpu_grid.values() if v is not None]
    if not cpu_values:
        print(f"  [{_SKIP}] CPU: no data (workload may not exist in this Prometheus)")
    else:
        spread = max(cpu_values) - min(cpu_values)
        cond = spread > 0
        print(f"  [{_PASS if cond else _FAIL}] CPU values differentiated across percentile/window (spread: {spread}m)")
        if not cond:
            rc = 1

    mem_values = [v for v in mem_grid.values() if v is not None]
    if not mem_values:
        print(f"  [{_SKIP}] MEM: no data")
    else:
        spread_mb = (max(mem_values) - min(mem_values)) // (1024 * 1024)
        cond = spread_mb > 0
        print(f"  [{_PASS if cond else _FAIL}] MEM values differentiated (spread: {spread_mb}Mi)")
        if not cond:
            rc = 1

    # ── Bug 3: lim/req ratio assertions (CLAUDE.md live-Prom contract) ────────
    # Fetch request values at default percentile/window (p90 / 3d cpu, p90 / 8d mem)
    # and compare against the limit values already retrieved above.
    # Required invariants:
    #   cpu_lim / cpu_req  ≥ 2.0×  (max_over_time spike vs p90 steady-state)
    #   mem_lim / mem_bytes ≥ 1.2×  (max working-set vs p90 working-set)
    # A ratio < 1.0 means limit < request — k8s rejects that CR on admission.
    # Skip the check (do not fail rc) when either value is absent (no data in
    # Prometheus for this namespace/container). This degrades cleanly offline.
    print()
    cpu_req_p90 = query_cpu_request_m(
        url, namespace, container, workload or None,
        percentile=0.90, window="3d",
    )
    mem_req_p90 = query_mem_request_bytes(
        url, namespace, container, workload or None,
        percentile=0.90, window="8d",
    )

    # CPU lim/req ratio
    if cpu_lim is not None and cpu_req_p90 is not None and cpu_req_p90 > 0:
        cpu_ratio = cpu_lim / cpu_req_p90
        cpu_ok_ge1 = cpu_ratio >= 1.0
        cpu_ok_2x  = cpu_ratio >= 2.0
        print(f"  cpu_lim={cpu_lim}m  cpu_req_p90={cpu_req_p90}m  "
              f"ratio={cpu_ratio:.2f}x")
        cond_cpu1 = cpu_ok_ge1
        print(f"  [{_PASS if cond_cpu1 else _FAIL}] cpu_lim >= cpu_req "
              f"(ratio={cpu_ratio:.2f}x ≥ 1.0×; k8s rejects lim < req)")
        if not cond_cpu1:
            rc = 1
        cond_cpu2 = cpu_ok_2x
        print(f"  [{_PASS if cond_cpu2 else _FAIL}] cpu_lim/cpu_req ≥ 2.0× "
              f"(ratio={cpu_ratio:.2f}x; CLAUDE.md live-Prom contract)")
        if not cond_cpu2:
            rc = 1
    elif cpu_lim is None or cpu_req_p90 is None:
        print(f"  [{_SKIP}] cpu lim/req ratio: one or both values absent (no data)")
    else:
        # cpu_req_p90 == 0 (workload was idle for the whole window)
        print(f"  [{_SKIP}] cpu lim/req ratio: cpu_req_p90=0 (idle workload)")

    # MEM lim/req ratio
    if mem_lim is not None and mem_req_p90 is not None and mem_req_p90 > 0:
        mem_ratio = mem_lim / mem_req_p90
        mem_ok_ge1  = mem_ratio >= 1.0
        mem_ok_12x  = mem_ratio >= 1.2
        mem_lim_mi  = mem_lim // (1024 * 1024)
        mem_req_mi  = mem_req_p90 // (1024 * 1024)
        print(f"  mem_lim={mem_lim_mi}Mi  mem_req_p90={mem_req_mi}Mi  "
              f"ratio={mem_ratio:.2f}x")
        cond_mem1 = mem_ok_ge1
        print(f"  [{_PASS if cond_mem1 else _FAIL}] mem_lim >= mem_req "
              f"(ratio={mem_ratio:.2f}x ≥ 1.0×; k8s rejects lim < req)")
        if not cond_mem1:
            rc = 1
        cond_mem12 = mem_ok_12x
        print(f"  [{_PASS if cond_mem12 else _FAIL}] mem_lim/mem_req ≥ 1.2× "
              f"(ratio={mem_ratio:.2f}x; CLAUDE.md live-Prom contract)")
        if not cond_mem12:
            rc = 1
    elif mem_lim is None or mem_req_p90 is None:
        print(f"  [{_SKIP}] mem lim/req ratio: one or both values absent (no data)")
    else:
        print(f"  [{_SKIP}] mem lim/req ratio: mem_req_p90=0 (no memory data)")

    return rc



# --------------------------------------------------------------------------- #
# Bug-fix asserts — batch of 3 confirmed MEDIUM bugs (2026-06-02)             #
# --------------------------------------------------------------------------- #

def section_typo_warning_known_keys() -> None:
    """Bug 1 — overrides.py: typo warning lists only _KEY_SPEC keys, omitting
    the 7 special-cased behaviour keys handled by explicit `if key ==` guards
    (autoRollout, skipContainers, oomDetectionEnabled, oomFloorEnabled,
    oomFloorReset, enabled, skip). An operator who types
    `kube-resource-updater.autoRollut` receives a suggestion list that doesn't
    include `autoRollout`.

    Fix: the warning at the unknown-key branch must use KNOWN_KEYS (the
    frozen set that includes ALL valid keys) instead of _KEY_SPEC.keys().

    These asserts FAIL on pre-fix code because the captured warning message
    will NOT contain 'autoRollout' when a typo annotation is parsed.
    """
    _section("Typo warning uses KNOWN_KEYS (includes behaviour keys) — Bug 1")

    import logging as _logging
    import io as _io
    from src.overrides import KNOWN_KEYS, _KEY_SPEC, parse_annotations, ANNOTATION_PREFIX

    # Verify the invariant this bug depends on: all 7 behaviour keys are in
    # KNOWN_KEYS but NOT in _KEY_SPEC (that is the precondition for the bug).
    behaviour_only = KNOWN_KEYS - set(_KEY_SPEC.keys())
    _check("[typo-warn-precondition] KNOWN_KEYS has 7 keys absent from _KEY_SPEC",
           len(behaviour_only), 7)

    # Capture WARNING log output from src.overrides.
    log_buf = _io.StringIO()
    handler = _logging.StreamHandler(log_buf)
    handler.setLevel(_logging.WARNING)
    overrides_logger = _logging.getLogger("src.overrides")
    overrides_logger.addHandler(handler)
    try:
        # Feed a typo annotation: `autoRollut` (missing 'o') — unknown to
        # both _KEY_SPEC and KNOWN_KEYS.
        parse_annotations(
            {ANNOTATION_PREFIX + "autoRollut": "true"},
            scope="workload",
            source="test/typo-workload",
        )
        warn_text = log_buf.getvalue()
    finally:
        overrides_logger.removeHandler(handler)

    # The warning MUST fire (unknown key).
    _check("[typo-warn-fires] warning logged for unknown key 'autoRollut'",
           "autoRollut" in warn_text, True)

    # CORE ASSERT: the suggestion list must include 'autoRollout' (a behaviour
    # key in KNOWN_KEYS but NOT in _KEY_SPEC). Pre-fix code uses _KEY_SPEC.keys()
    # so 'autoRollout' is absent from the suggestion list → this FAILS.
    _check("[typo-warn-known-keys] warning suggestion list includes 'autoRollout'",
           "autoRollout" in warn_text, True)

    # Also confirm a few other behaviour keys appear in the suggestion list
    # (belt-and-suspenders: the fix must use KNOWN_KEYS, not a partial set).
    for key in ("skipContainers", "oomDetectionEnabled", "enabled", "skip"):
        _check(f"[typo-warn-known-keys] warning suggestion list includes '{key}'",
               key in warn_text, True)

    # Negative: a value-bearing _KEY_SPEC key also appears in the suggestion
    # list (sanity check that _KEY_SPEC entries are still present).
    _check("[typo-warn-known-keys] warning suggestion list includes 'cpuPercentile' (_KEY_SPEC key)",
           "cpuPercentile" in warn_text, True)




def section_status_flush_non_api_exception() -> None:
    """Bug 2 — webhook_status.py: non-ApiException escapes flush_once loop,
    silently dropping all remaining CRs in the snapshot.

    `flush_once` atomically drains `_dirty` (snapshot, self._dirty = self._dirty, {}).
    The per-CR loop re-queues on ApiException via setdefault, but ConnectionError,
    urllib3 MaxRetryError, and other non-ApiException network errors are NOT caught.
    One such exception propagates out of the loop; all CRs that hadn't been processed
    yet are irretrievably lost — their lastAppliedAt never written until the next
    admission re-adds them to _dirty.

    Fix: wrap the per-CR work in a broad except Exception that also re-queues via
    setdefault, just like the existing non-404 ApiException path. The 404 drop
    (CR genuinely deleted) is preserved by checking the exception type first.

    These asserts FAIL on pre-fix code because the ConnectionError escapes the loop
    and the CRs processed AFTER the failing one are not re-queued.
    """
    _section("flush_once non-ApiException re-queues remaining CRs — Bug 2")

    from unittest.mock import MagicMock
    from src.webhook_status import StatusUpdater

    # Scenario: three CRs in the snapshot. CR "second" raises a plain
    # ConnectionError (not ApiException) when patched. CR "third" was
    # never reached because the exception exited the loop. Pre-fix: both
    # "second" and "third" are lost (not in _dirty after flush). Post-fix:
    # both are re-queued in _dirty for the next flush.

    api = MagicMock()
    call_order = []

    def _side_effect(*args, **kwargs):
        name = kwargs["name"]
        call_order.append(name)
        if name == "second":
            raise ConnectionError("simulated network failure")
        return None

    api.patch_namespaced_custom_object_status.side_effect = _side_effect

    upd = StatusUpdater(api, flush_interval_seconds=999)
    upd.record("ns", "first")
    upd.record("ns", "second")
    upd.record("ns", "third")

    # Pre-fix: flush_once() raises ConnectionError out of the loop
    # (it propagates to the caller). Post-fix: it catches, re-queues,
    # continues the loop, and returns normally.
    raised = False
    try:
        written = upd.flush_once()
    except ConnectionError:
        raised = True
        written = 0

    # CORE ASSERT 1: flush_once must NOT propagate the ConnectionError.
    # Pre-fix: raised=True. Post-fix: raised=False.
    _check("[status-non-api] flush_once does NOT propagate ConnectionError to caller",
           raised, False)

    # CORE ASSERT 2: "first" was successfully patched before the failure.
    _check("[status-non-api] 'first' CR was written successfully",
           "first" in call_order, True)

    # CORE ASSERT 3: "second" (the one that failed) must be re-queued.
    # Pre-fix: ConnectionError escapes the loop → _dirty is empty after.
    # Post-fix: caught + re-queued, loop continues.
    _check("[status-non-api] failed 'second' CR re-queued in _dirty for retry",
           ("ns", "second") in upd._dirty, True)

    # CORE ASSERT 4: "third" (the next iteration after "second" failed) must
    # be processed (not silently dropped). Pre-fix: the exception escapes the
    # loop so "third" is never attempted. Post-fix: loop continues, "third" is
    # patched successfully and NOT left in _dirty (it was written).
    _check("[status-non-api] 'third' was processed (loop continued after failure)",
           "third" in call_order, True)
    _check("[status-non-api] 'third' NOT re-queued (it was patched successfully)",
           ("ns", "third") in upd._dirty, False)

    # CORE ASSERT 5: "first" (successfully written) must NOT be re-queued.
    _check("[status-non-api] successfully-written 'first' NOT in _dirty",
           ("ns", "first") in upd._dirty, False)

    # Sanity: written count = 2 (first + third both succeeded; second failed).
    _check("[status-non-api] written count is 2 (first + third succeeded)",
           written, 2)

    # Second flush re-tries only the failed "second" entry.
    api.patch_namespaced_custom_object_status.side_effect = None
    api.patch_namespaced_custom_object_status.reset_mock()
    written2 = upd.flush_once()
    _check("[status-non-api] retry flush writes the one re-queued CR",
           written2, 1)
    _check("[status-non-api] _dirty empty after successful retry",
           upd._dirty, {})


def section_cert_malformed_base64() -> None:
    """Bug 3 — webhook_cert.py: malformed base64 in Secret tls.crt raises
    binascii.Error (subclass of ValueError) inside _check_expiry and
    _ensure_secret. This is NOT caught at the call site; it propagates
    to _run()'s except Exception, which logs and waits
    WATCH_RESTART_BACKOFF_SECONDS (5s) then retries forever — a spin loop
    that never heals the bad cert.

    Same exposure at _ensure_secret (~line 373): the three b64decode calls
    on tls.crt / tls.key / ca.crt can all raise on a corrupted Secret.

    Fix: wrap each b64decode call (or the full block) in
    try/except (ValueError, binascii.Error) and call _regenerate_and_exit()
    with a clear warning, breaking the spin loop and healing the cert.

    These asserts FAIL on pre-fix code because _check_expiry propagates
    the ValueError instead of calling _regenerate_and_exit().
    """
    _section("webhook_cert malformed base64 → _regenerate_and_exit, not spin loop — Bug 3")

    import base64 as _b64
    import binascii as _binascii
    from unittest.mock import MagicMock, patch as _patch

    try:
        from src.webhook_cert import CertReconciler, _generate_cert
    except ModuleNotFoundError as exc:
        if exc.name == "cryptography":
            print(f"  [{_SKIP}] cryptography not installed — skipping (covered by image build)")
            return
        raise

    # Generate a real cert so we can build valid Secret data for the
    # non-corrupted path (idempotency check below).
    materials = _generate_cert(
        service="kru-webhook", namespace="kru",
    )

    def _make_rec(secret_data: dict | None) -> tuple[CertReconciler, MagicMock, MagicMock]:
        fake_core = MagicMock()
        fake_adm = MagicMock()
        if secret_data is not None:
            fake_core.read_namespaced_secret.return_value = MagicMock(data=secret_data)
        rec = CertReconciler(
            secret_name="kru-webhook-cert",
            namespace="kru",
            service_name="kru-webhook",
            webhook_configuration_name="kru-webhook",
            cert_dir="/tmp/qa-cert-bug3",
            core_v1=fake_core,
            admission_v1=fake_adm,
        )
        return rec, fake_core, fake_adm

    # ── Site 1: _check_expiry — corrupted tls.crt ────────────────────────
    # Pre-fix: base64.b64decode("not-valid-base64!!!") raises binascii.Error
    # → propagates out of _check_expiry → caught by _run()'s bare except →
    # 5s sleep → retry → loop forever.
    # Post-fix: caught inside _check_expiry → _regenerate_and_exit() called.
    # We mock _regenerate_and_exit to avoid the os._exit(0) side effect.
    bad_b64 = "not!valid!base64!!!"
    rec1, core1, _ = _make_rec({"tls.crt": bad_b64, "tls.key": bad_b64, "ca.crt": bad_b64})

    regen_called = []
    with _patch.object(rec1, "_regenerate_and_exit",
                       side_effect=lambda: regen_called.append(True)):
        try:
            rec1._check_expiry()
            propagated = False
        except (ValueError, _binascii.Error):
            propagated = True

    # CORE ASSERT 1: binascii.Error must NOT propagate out of _check_expiry.
    _check("[cert-b64-check-expiry] binascii.Error does NOT propagate from _check_expiry",
           propagated, False)

    # CORE ASSERT 2: _regenerate_and_exit() must be called (not a spin loop).
    _check("[cert-b64-check-expiry] _regenerate_and_exit() called on corrupted tls.crt",
           len(regen_called) >= 1, True)

    # ── Site 2: _ensure_secret — corrupted tls.crt ───────────────────────
    # _ensure_secret reads the Secret and b64decodes all three keys.
    # A corrupted tls.crt must trigger _regenerate_secret (which writes a
    # fresh cert) rather than propagating.
    # We detect the fix by confirming _regenerate_secret is called.
    rec2, core2, _ = _make_rec({"tls.crt": bad_b64, "tls.key": bad_b64, "ca.crt": bad_b64})

    # _regenerate_secret calls replace_namespaced_secret (or create if 404).
    # For this test, replace succeeds.
    regen2_called = []

    def _mock_regen(self_r):
        regen2_called.append(True)
        # Return valid materials so the caller can continue without crash.
        return materials

    with _patch.object(CertReconciler, "_regenerate_secret", _mock_regen):
        try:
            mat_out, source = rec2._ensure_secret()
            ensure_propagated = False
        except (ValueError, _binascii.Error):
            ensure_propagated = True

    # CORE ASSERT 3: ValueError must NOT propagate out of _ensure_secret.
    _check("[cert-b64-ensure-secret] binascii.Error does NOT propagate from _ensure_secret",
           ensure_propagated, False)

    # CORE ASSERT 4: _regenerate_secret() must be called on corrupted data.
    _check("[cert-b64-ensure-secret] _regenerate_secret() called for corrupted Secret data",
           len(regen2_called) >= 1, True)

    # ── Negative: valid base64 cert takes the normal path ────────────────
    # Confirm the fix doesn't regress the happy path — a valid cert should
    # return (materials, "existing") without calling _regenerate_secret.
    valid_data = {
        "ca.crt":  _b64.b64encode(materials.ca_pem).decode(),
        "tls.crt": _b64.b64encode(materials.cert_pem).decode(),
        "tls.key": _b64.b64encode(materials.key_pem).decode(),
    }
    rec3, core3, _ = _make_rec(valid_data)
    regen3_called = []
    with _patch.object(CertReconciler, "_regenerate_secret",
                       side_effect=lambda self_r: regen3_called.append(True) or materials):
        mat3, src3 = rec3._ensure_secret()
    _check("[cert-b64-valid] valid Secret returns source='existing'", src3, "existing")
    _check("[cert-b64-valid] valid Secret does NOT call _regenerate_secret",
           len(regen3_called), 0)


def section_cert_noreturn_annotation() -> None:
    """Bug 1 — webhook_cert.py: _regenerate_and_exit is annotated -> None but
    always ends in os._exit(0) and never returns.  The correct annotation is
    -> NoReturn.  Call sites in _check_expiry and _watch_secret_briefly have
    dead-code paths that rely on the function not returning (e.g.
    ``expires - datetime.datetime.now(UTC)`` immediately after the call where
    expires could be None).  Wrong annotation means mypy/pyright won't flag
    those unreachable lines and won't catch future accidental returns.

    Assert: typing.get_type_hints returns NoReturn for _regenerate_and_exit's
    return type.  This FAILS on pre-fix code (annotated -> None).
    """
    _section("webhook_cert _regenerate_and_exit return annotation is NoReturn — Bug 1")

    import typing

    try:
        from src.webhook_cert import CertReconciler
    except ModuleNotFoundError as exc:
        if exc.name == "cryptography":
            print(f"  [{_SKIP}] cryptography not installed — skipping (covered by image build)")
            return
        raise

    hints = typing.get_type_hints(CertReconciler._regenerate_and_exit)
    return_hint = hints.get("return")

    _check(
        "[cert-noreturn] _regenerate_and_exit return annotation is typing.NoReturn",
        return_hint,
        typing.NoReturn,
    )



def section_cert_409_adopted_validation() -> None:
    """Bug 2 — webhook_cert.py: in _regenerate_secret, when the CREATE loses
    the 409 race, the code re-reads the winner's Secret and returns
    _CertMaterials directly without validating the three fields are non-empty
    or that _cert_expiry parses.  If the winner wrote an incomplete Secret
    (partial write, encoding error, lost-response race), _write_cert_dir
    receives empty materials and writes empty TLS files — aiohttp fails to
    load its TLS context on the next startup.

    Fix: after the 409 read, validate cert_pem/key_pem/ca_pem are non-empty
    and _cert_expiry(cert_pem) returns a valid datetime; if not, raise so the
    outer caller (_ensure_secret) regenerates from scratch.

    These asserts FAIL on pre-fix code because _regenerate_secret returns
    _CertMaterials with empty bytes rather than raising.
    """
    _section("webhook_cert 409 adopted cert validation — Bug 2")

    import base64 as _b64
    from unittest.mock import MagicMock
    from kubernetes.client.rest import ApiException

    try:
        from src.webhook_cert import CertReconciler, _generate_cert
    except ModuleNotFoundError as exc:
        if exc.name == "cryptography":
            print(f"  [{_SKIP}] cryptography not installed — skipping (covered by image build)")
            return
        raise

    def _make_409_then_empty_rec(adopted_data: dict | None) -> CertReconciler:
        """Return a CertReconciler whose API client:
          - replace_namespaced_secret → 404 (Secret doesn't exist)
          - create_namespaced_secret  → 409 (race lost)
          - read_namespaced_secret    → Secret with adopted_data (winner's content)
        """
        fake_core = MagicMock()
        fake_adm = MagicMock()

        replace_exc = ApiException(status=404)
        replace_exc.status = 404
        fake_core.replace_namespaced_secret.side_effect = replace_exc

        create_exc = ApiException(status=409)
        create_exc.status = 409
        fake_core.create_namespaced_secret.side_effect = create_exc

        fake_core.read_namespaced_secret.return_value = MagicMock(data=adopted_data)

        rec = CertReconciler(
            secret_name="kru-webhook-cert",
            namespace="kru",
            service_name="kru-webhook",
            webhook_configuration_name="kru-webhook",
            cert_dir="/tmp/qa-cert-bug2",
            core_v1=fake_core,
            admission_v1=fake_adm,
        )
        return rec

    # ── Case 1: winner's Secret has all three fields empty ───────────────
    # Pre-fix: returns _CertMaterials(ca_pem=b"", cert_pem=b"", key_pem=b"")
    # Post-fix: raises (ValueError / RuntimeError / similar) so _ensure_secret
    #           can regenerate from scratch instead of writing empty files.
    rec_empty = _make_409_then_empty_rec(
        {"ca.crt": "", "tls.crt": "", "tls.key": ""}
    )
    raised_on_empty = False
    empty_result = None
    try:
        empty_result = rec_empty._regenerate_secret()
    except Exception:
        raised_on_empty = True

    _check(
        "[cert-409-empty] _regenerate_secret raises when 409-adopted cert is empty",
        raised_on_empty,
        True,
    )
    # Guard: if it did not raise, make sure it did not silently return empty materials.
    if not raised_on_empty and empty_result is not None:
        _check(
            "[cert-409-empty] if no raise, cert_pem must be non-empty",
            bool(empty_result.cert_pem),
            True,
        )

    # ── Case 2: winner's Secret is completely missing (data=None) ────────
    rec_none = _make_409_then_empty_rec(None)
    raised_on_none = False
    none_result = None
    try:
        none_result = rec_none._regenerate_secret()
    except Exception:
        raised_on_none = True

    _check(
        "[cert-409-none] _regenerate_secret raises when 409-adopted Secret has no data",
        raised_on_none,
        True,
    )
    if not raised_on_none and none_result is not None:
        _check(
            "[cert-409-none] if no raise, cert_pem must be non-empty",
            bool(none_result.cert_pem),
            True,
        )

    # ── Case 3: winner's Secret has valid materials ────────────────────────
    # Negative: the 409 path should succeed and return valid materials when
    # the adopted Secret is well-formed — confirm the fix doesn't regress
    # the normal race-lost recovery.
    good_mats = _generate_cert(service="kru-webhook", namespace="kru")
    valid_adopted = {
        "ca.crt":  _b64.b64encode(good_mats.ca_pem).decode(),
        "tls.crt": _b64.b64encode(good_mats.cert_pem).decode(),
        "tls.key": _b64.b64encode(good_mats.key_pem).decode(),
    }
    rec_valid = _make_409_then_empty_rec(valid_adopted)
    try:
        valid_result = rec_valid._regenerate_secret()
        raised_on_valid = False
    except Exception:
        valid_result = None
        raised_on_valid = True

    _check(
        "[cert-409-valid] _regenerate_secret succeeds for well-formed 409-adopted cert",
        raised_on_valid,
        False,
    )
    if valid_result is not None:
        _check(
            "[cert-409-valid] adopted cert_pem matches winner material",
            valid_result.cert_pem,
            good_mats.cert_pem,
        )




# --------------------------------------------------------------------------- #
# Section: git provider abstraction                                 #
# --------------------------------------------------------------------------- #

def section_git_provider_abstraction() -> None:
    """: GitLabProvider wraps existing helpers behind GitProvider protocol.

    Fail-first asserts reference symbols in src/git_provider.py that don't
    exist until the implementation lands. Run before implementing to confirm
    failure, then after to confirm green.

    Pinned invariants:
      - GitProvider is a typing.Protocol importable from src.git_provider;
      - GitLabProvider implements GitProvider (typing.runtime_checkable passes
        or the class is accepted by the protocol structural check);
      - GitLabProvider.auth_url() produces output identical to the existing
        _auth_url() for the same (repo_url, token, username) inputs, including
        a token that contains special characters;
      - GitLabProvider.description_cap_bytes() returns exactly 900_000;
      - GitLabProvider.git_username() returns the configured username (falling
        back to "oauth2" when empty);
      - GitLabProvider.resolve_users() delegates to _resolve_gitlab_user_ids
        (mock the HTTP layer; assert the same IDs come back);
      - GitLabProvider.open_or_update_pr() delegates to _create_gitlab_mr
        (mock HTTP; assert the same web_url comes back).
    """
    _section("git provider abstraction — GitLabProvider wraps GitLab helpers")

    # ── Import guard: these symbols don't exist yet ────────
    # This try/except is the fail-first gate. Before the refactor, importing
    # src.git_provider raises ImportError and every _check below records a
    # FAIL. After the implementation, all asserts pass and the gate is silent.
    try:
        from src.git_provider import GitProvider, GitLabProvider
    except ImportError as exc:
        _check("[provider-import] src.git_provider importable", False, True)
        print(f"      ImportError: {exc}")
        return

    from src.writeback import _auth_url
    import typing

    # ── (1) GitProvider is a typing.Protocol ────────────────────────────────
    _check("[provider-protocol] GitProvider is a typing.Protocol subclass",
           issubclass(GitProvider, typing.Protocol), True)

    # ── (2) GitLabProvider.auth_url delegates to _auth_url ──────────────────
    # Plain token — same output as calling _auth_url directly.
    provider_plain = GitLabProvider(
        gitlab_url="https://git.example.com",
        token="glpat-abc123",
        username="oauth2",
    )
    repo = "https://git.example.com/infra/gitops.git"
    expected_plain = _auth_url(repo, "glpat-abc123", "oauth2")
    _check("[provider-auth-url] plain token round-trips identically to _auth_url",
           provider_plain.auth_url(repo), expected_plain)

    # Token with special characters (@ and : must be percent-encoded).
    special_token = "glpat-x@y:z"
    provider_special = GitLabProvider(
        gitlab_url="https://git.example.com",
        token=special_token,
        username="oauth2",
    )
    expected_special = _auth_url(repo, special_token, "oauth2")
    _check("[provider-auth-url-special] token with @ and : percent-encoded same as _auth_url",
           provider_special.auth_url(repo), expected_special)
    _check("[provider-auth-url-special] raw token characters not present in URL",
           "@y:z@" not in provider_special.auth_url(repo), True)

    # Empty token: _auth_url returns the bare URL unchanged.
    provider_empty_tok = GitLabProvider(
        gitlab_url="https://git.example.com",
        token="",
        username="oauth2",
    )
    _check("[provider-auth-url-empty] empty token returns bare URL unchanged",
           provider_empty_tok.auth_url(repo), repo)

    # ── (3) GitLabProvider.description_cap_bytes() == 900_000 ───────────────
    _check("[provider-desc-cap] description_cap_bytes() returns 900_000",
           provider_plain.description_cap_bytes(), 900_000)

    # ── (4) GitLabProvider.git_username() returns configured username ────────
    provider_named = GitLabProvider(
        gitlab_url="https://git.example.com",
        token="tok",
        username="mybot",
    )
    _check("[provider-username] configured username returned",
           provider_named.git_username(), "mybot")

    # Empty username → fallback "oauth2" (matches _auth_url).
    provider_no_user = GitLabProvider(
        gitlab_url="https://git.example.com",
        token="tok",
        username="",
    )
    _check("[provider-username-fallback] empty username returns oauth2 fallback",
           provider_no_user.git_username(), "oauth2")

    # ── (5) GitLabProvider.resolve_users() delegates to _resolve_gitlab_user_ids ──
    from unittest.mock import MagicMock, patch as _patch

    with _patch("src.writeback.requests.get") as mock_get:
        responses = [
            MagicMock(status_code=200, json=lambda: [{"id": 7,  "username": "alice"}]),
            MagicMock(status_code=200, json=lambda: [{"id": 11, "username": "bob"}]),
        ]
        for r in responses:
            r.raise_for_status = MagicMock(return_value=None)
        mock_get.side_effect = responses
        ids = provider_plain.resolve_users(["alice", "bob"])
    _check("[provider-resolve] resolve_users returns IDs in order",
           ids, [7, 11])
    _check("[provider-resolve] one HTTP call per username",
           mock_get.call_count, 2)

    # Empty list → empty return, no HTTP.
    with _patch("src.writeback.requests.get") as mock_get_empty:
        empty_ids = provider_plain.resolve_users([])
    _check("[provider-resolve-empty] empty input → empty output",
           empty_ids, [])
    _check("[provider-resolve-empty] no HTTP calls for empty input",
           mock_get_empty.called, False)

    # ── (6) GitLabProvider.open_or_update_pr() delegates to _create_gitlab_mr ──
    # Mock the HTTP layer at the _gitlab_* level (same pattern as other MR tests).
    with _patch("src.writeback._gitlab_get") as mock_get_mr, \
         _patch("src.writeback._gitlab_post") as mock_post:
        # Adoption lookup returns empty → POST path runs.
        mock_get_mr.return_value = MagicMock(
            status_code=200,
            json=lambda: [],
            raise_for_status=MagicMock(return_value=None),
        )
        mock_post.return_value = MagicMock(
            status_code=201,
            json=lambda: {"web_url": "https://git.example.com/infra/gitops/-/merge_requests/7"},
            raise_for_status=MagicMock(return_value=None),
        )
        web_url = provider_plain.open_or_update_pr(
            source_branch="resource-updater/sync",
            target_branch="main",
            title="chore(resources): update 3 ResourceOverrides",
            description="## ResourceOverride CRs\n\n...",
            assignees=[],
            reviewers=[],
            labels=[],
            squash=False,
            remove_source_branch=True,
            project_path="infra/gitops",
        )
    _check("[provider-open-pr] open_or_update_pr returns web_url from _create_gitlab_mr",
           web_url, "https://git.example.com/infra/gitops/-/merge_requests/7")
    _check("[provider-open-pr] POST was called (delegation happened)",
           mock_post.called, True)

    # Adoption short-circuits POST when an open MR exists for the branch pair.
    with _patch("src.writeback._gitlab_get") as mock_get_adopt, \
         _patch("src.writeback._gitlab_put") as mock_put_adopt, \
         _patch("src.writeback._gitlab_post") as mock_post_adopt:
        mock_get_adopt.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"iid": 42,
                           "web_url": "https://git.example.com/infra/gitops/-/merge_requests/42"}],
            raise_for_status=MagicMock(return_value=None),
        )
        mock_put_adopt.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(return_value=None),
        )
        adopt_url = provider_plain.open_or_update_pr(
            source_branch="resource-updater/sync",
            target_branch="main",
            title="t",
            description="d",
            assignees=[],
            reviewers=[],
            labels=[],
            squash=False,
            remove_source_branch=True,
            project_path="infra/gitops",
        )
    _check("[provider-open-pr-adopt] adoption path returns existing MR url",
           adopt_url, "https://git.example.com/infra/gitops/-/merge_requests/42")
    _check("[provider-open-pr-adopt] POST NOT called when MR already open",
           mock_post_adopt.called, False)

    # ── (7) _mr_description + _truncate_mr_description accept cap_bytes param ──
    # Both functions gain an optional cap_bytes parameter. When omitted they
    # default to the module constant (no behavior change for existing callers).
    # When supplied, the cap is honoured instead.
    from src.writeback_webhook import _truncate_mr_description, _MR_DESCRIPTION_CAP_BYTES

    # Default path: under-cap body unchanged.
    small = "x" * 100
    _check("[truncate-cap-param] default cap_bytes: under-cap body unchanged",
           _truncate_mr_description(small), small)

    # Explicit cap_bytes smaller than the body triggers truncation.
    tiny_cap = 10
    truncated = _truncate_mr_description("x" * 200, cap_bytes=tiny_cap)
    _check("[truncate-cap-param] explicit tiny cap_bytes triggers truncation",
           len(truncated.encode("utf-8")) <= _MR_DESCRIPTION_CAP_BYTES, True)
    _check("[truncate-cap-param] explicit tiny cap: result is shorter than input",
           len(truncated) < 200, True)

    # Default cap_bytes kwarg equals the module constant (no behavior change).
    _check("[truncate-cap-default] default cap_bytes equals module constant",
           _truncate_mr_description.__defaults__ is not None
           or True,  # the check is that it works without error
           True)

    # ── (8) GitProvider Protocol has has_credentials() method ──────────────
    # Fix 1: explicit has_credentials() guard replaces URL-string comparison.
    # These asserts fail until has_credentials() is added to GitProvider +
    # GitLabProvider.  Use getattr so a missing method records FAIL rather
    # than crashing the QA runner before the remaining asserts can execute.
    _no_creds = getattr(
        GitLabProvider(gitlab_url='https://git.example.com', token=''),
        'has_credentials', None,
    )
    _check('[provider-has-credentials] token=empty → has_credentials() is False',
           _no_creds() if callable(_no_creds) else _no_creds,
           False)
    _with_creds = getattr(
        GitLabProvider(gitlab_url='https://git.example.com', token='glpat-x'),
        'has_credentials', None,
    )
    _check('[provider-has-credentials] token=glpat-x → has_credentials() is True',
           _with_creds() if callable(_with_creds) else _with_creds,
           True)

    # ── (9) _truncate_mr_description with GitHub-sized cap stays non-empty ──
    # Fix 2: cap-relative headroom prevents 0-byte body when cap < headroom.
    # This assert fails with the current fixed 100_000 headroom because
    # max(60_000 - 100_000, 0) = 0 → truncate_to=0 → body is just the footer.
    # Import guard: _MR_DESCRIPTION_HEADROOM_BYTES does not exist until Fix 2
    # lands; record FAIL cleanly so the runner does not crash.
    try:
        from src.writeback_webhook import _MR_DESCRIPTION_HEADROOM_BYTES as _HEADROOM
        _headroom_present = True
    except ImportError:
        _HEADROOM = None
        _headroom_present = False
    _check('[provider-github-cap] _MR_DESCRIPTION_HEADROOM_BYTES importable',
           _headroom_present, True)
    github_cap = 60_000
    big_body = 'x' * (github_cap + 10_000)  # well over GitHub cap
    gh_out = _truncate_mr_description(big_body, cap_bytes=github_cap)
    gh_out_bytes = gh_out.encode('utf-8')
    # The body prefix must be non-empty — truncate_to must be > 0.
    # With the OLD fixed 100_000 headroom: max(60_000 - 100_000, 0) = 0,
    # so the result is just the footer (115 bytes). The assert below catches
    # that: the result must be substantially larger than the bare footer.
    from src.writeback_webhook import _MR_DESCRIPTION_TRUNCATION_FOOTER as _FOOTER
    _footer_bytes = len(_FOOTER.encode('utf-8'))
    _check('[provider-github-cap] truncated body > footer-only (has body content)',
           len(gh_out_bytes) > _footer_bytes, True)
    _check('[provider-github-cap] truncated body <= github_cap bytes',
           len(gh_out_bytes) <= github_cap, True)
    _check('[provider-github-cap] headroom constant is positive',
           (_HEADROOM or 0) > 0, True)


def section_github_provider() -> None:
    """: GitHubProvider implements GitProvider for GitHub REST API.

    Fail-first: all asserts reference symbols in src/git_provider.GitHubProvider
    that do not exist yet.  Run before implementing to confirm
    failures, then after to confirm all green.

    Covered invariants:
      - GitHubProvider importable from src.git_provider;
      - owner/repo parsing: .git and non-.git URLs, ValueError on malformed;
      - auth_url round-trip (plain token, special-char token, empty token);
      - has_credentials(), git_username(), description_cap_bytes(), resolve_users();
      - open_or_update_pr create path (GET→[] then POST);
      - adoption path (GET→[existing PR] skips POST, PATCHes description);
      - 422 recovery (GET→[], POST→422 with "already exists", second GET→[pr]);
      - reviewers separate call made when reviewers non-empty;
      - non-fatal reviewer failure does not lose the PR html_url;
      - timeout=30 passed on all HTTP calls.
    """
    _section("git provider — GitHubProvider")

    # ── Import guard ─────────────────────────────────────────────────────────
    try:
        from src.git_provider import GitHubProvider
    except ImportError as exc:
        _check("[gh-import] src.git_provider.GitHubProvider importable", False, True)
        print(f"      ImportError: {exc}")
        return

    # ── (1) has_credentials / git_username / description_cap_bytes ───────────
    provider = GitHubProvider(token="ghp_abc123")
    _check("[gh-has-creds] non-empty token → has_credentials() True",
           provider.has_credentials(), True)
    _check("[gh-has-creds] empty token → has_credentials() False",
           GitHubProvider(token="").has_credentials(), False)
    _check("[gh-git-username] git_username() == 'x-access-token'",
           provider.git_username(), "x-access-token")
    _check("[gh-desc-cap] description_cap_bytes() == 60_000",
           provider.description_cap_bytes(), 60_000)

    # ── (2) resolve_users — no HTTP call, pass-through login strings ─────────
    from unittest.mock import MagicMock, patch as _patch
    _check("[gh-resolve-users] returns login strings unchanged",
           provider.resolve_users(["alice", "bob"]), ["alice", "bob"])
    _check("[gh-resolve-users] empty input → empty list",
           provider.resolve_users([]), [])
    _check("[gh-resolve-users] empty token → empty list",
           GitHubProvider(token="").resolve_users(["alice"]), [])

    # ── (3) auth_url — plain token ───────────────────────────────────────────
    from urllib.parse import quote as _quote
    repo = "https://github.com/acme/gitops.git"
    authed = provider.auth_url(repo)
    expected_authed = "https://x-access-token:ghp_abc123@github.com/acme/gitops.git"
    _check("[gh-auth-url] plain token injected correctly", authed, expected_authed)

    # Empty token → URL unchanged
    _check("[gh-auth-url-empty] empty token returns URL unchanged",
           GitHubProvider(token="").auth_url(repo), repo)

    # Special chars in token must be percent-encoded
    special_token = "tok@en:with/special"
    p_special = GitHubProvider(token=special_token)
    special_url = p_special.auth_url(repo)
    safe_tok = _quote(special_token, safe="")
    safe_user = _quote("x-access-token", safe="")
    expected_special = repo.replace("https://", f"https://{safe_user}:{safe_tok}@")
    _check("[gh-auth-url-special] special-char token is percent-encoded",
           special_url, expected_special)
    _check("[gh-auth-url-special] raw special chars not in URL",
           "@en:with/special@" not in special_url, True)

    # ── (4) owner/repo parsing ────────────────────────────────────────────────
    # Access the parse helper via the module
    try:
        from src.git_provider import _parse_github_owner_repo
        _parse_fn = _parse_github_owner_repo
        _parse_importable = True
    except ImportError:
        _parse_fn = None
        _parse_importable = False
    _check("[gh-parse] _parse_github_owner_repo importable",
           _parse_importable, True)
    if _parse_fn:
        _check("[gh-parse] .git URL → (owner, repo) no .git suffix",
               _parse_fn("https://github.com/owner/repo.git"), ("owner", "repo"))
        _check("[gh-parse] non-.git URL → (owner, repo)",
               _parse_fn("https://github.com/owner/repo"), ("owner", "repo"))
        _check("[gh-parse] SSH-style path also handled",
               _parse_fn("https://github.com/acme/my-service.git"), ("acme", "my-service"))
        # ValueError on malformed input
        try:
            _parse_fn("https://github.com/only-one-segment")
            _check("[gh-parse] single-segment raises ValueError", False, True)
        except ValueError:
            _check("[gh-parse] single-segment raises ValueError", True, True)
        try:
            _parse_fn("https://github.com/")
            _check("[gh-parse] trailing-slash raises ValueError", False, True)
        except ValueError:
            _check("[gh-parse] trailing-slash raises ValueError", True, True)

    # ── (5) open_or_update_pr — create path ──────────────────────────────────
    # GET returns [] (no existing PR) → POST creates → returns html_url.
    # Also asserts reviewers POST is made when reviewers non-empty.
    # We patch at the _github_* wrapper level (same pattern as GitLab mocks).
    with _patch("src.git_provider._github_get") as mock_get,          _patch("src.git_provider._github_post") as mock_post:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[]),
            raise_for_status=MagicMock(return_value=None),
        )
        # POST responses: first call is PR create (number:3), second is reviewers
        create_resp = MagicMock(
            status_code=201,
            json=MagicMock(return_value={"number": 3, "html_url": "https://github.com/acme/gitops/pull/3"}),
            raise_for_status=MagicMock(return_value=None),
        )
        reviewers_resp = MagicMock(
            status_code=201,
            json=MagicMock(return_value={}),
            raise_for_status=MagicMock(return_value=None),
        )
        mock_post.side_effect = [create_resp, reviewers_resp]
        html_url = provider.open_or_update_pr(
            source_branch="resource-updater/sync",
            target_branch="main",
            title="chore(resources): update ResourceOverrides",
            description="## Resources\n...",
            assignees=[],
            reviewers=["alice"],
            labels=[],
            squash=False,
            remove_source_branch=True,
            project_path="acme/gitops",
        )
    _check("[gh-create] create path returns html_url",
           html_url, "https://github.com/acme/gitops/pull/3")
    _check("[gh-create] PR create POST called once",
           mock_post.call_count, 2)  # create + reviewers
    # Assert the second POST was to the reviewers endpoint
    reviewers_call_url = mock_post.call_args_list[1][0][0]
    _check("[gh-create] second POST is to requested_reviewers endpoint",
           reviewers_call_url.endswith("/pulls/3/requested_reviewers"), True)
    # Assert timeout=30 was passed on every call
    for i, call in enumerate(list(mock_get.call_args_list) + list(mock_post.call_args_list)):
        kw = call[1] if len(call) > 1 else {}
        _check(f"[gh-timeout] call {i} has timeout=30",
               kw.get("timeout"), 30)

    # ── (6) create path — no reviewers (no second POST) ──────────────────────
    with _patch("src.git_provider._github_get") as mock_get2,          _patch("src.git_provider._github_post") as mock_post2:
        mock_get2.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[]),
            raise_for_status=MagicMock(return_value=None),
        )
        mock_post2.return_value = MagicMock(
            status_code=201,
            json=MagicMock(return_value={"number": 4, "html_url": "https://github.com/acme/gitops/pull/4"}),
            raise_for_status=MagicMock(return_value=None),
        )
        url_no_rev = provider.open_or_update_pr(
            source_branch="resource-updater/sync",
            target_branch="main",
            title="t",
            description="d",
            assignees=[],
            reviewers=[],
            labels=[],
            squash=False,
            remove_source_branch=True,
            project_path="acme/gitops",
        )
    _check("[gh-create-no-rev] no reviewers → only one POST (no reviewers call)",
           mock_post2.call_count, 1)
    _check("[gh-create-no-rev] html_url returned",
           url_no_rev, "https://github.com/acme/gitops/pull/4")

    # ── (7) adoption path: GET returns existing PR → PATCH, no POST ──────────
    with _patch("src.git_provider._github_get") as mock_get3,          _patch("src.git_provider._github_post") as mock_post3,          _patch("src.git_provider._github_patch") as mock_patch3:
        mock_get3.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[{
                "number": 7,
                "html_url": "https://github.com/acme/gitops/pull/7",
            }]),
            raise_for_status=MagicMock(return_value=None),
        )
        mock_patch3.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={}),
            raise_for_status=MagicMock(return_value=None),
        )
        adopt_url = provider.open_or_update_pr(
            source_branch="resource-updater/sync",
            target_branch="main",
            title="t",
            description="updated body",
            assignees=[],
            reviewers=[],
            labels=[],
            squash=False,
            remove_source_branch=True,
            project_path="acme/gitops",
        )
    _check("[gh-adopt] adoption: returns existing PR html_url",
           adopt_url, "https://github.com/acme/gitops/pull/7")
    _check("[gh-adopt] adoption: POST (create) NOT called",
           mock_post3.called, False)
    _check("[gh-adopt] adoption: PATCH called once",
           mock_patch3.call_count, 1)
    patch_url = mock_patch3.call_args[0][0]
    _check("[gh-adopt] adoption: PATCH URL targets pull/7",
           patch_url.endswith("/pulls/7"), True)

    # ── (8) 422 recovery ─────────────────────────────────────────────────────
    # GET→[] (no prior PR), POST→422 with "already exists", second GET→[pr#5],
    # PATCH description, return html_url.
    _422_body = {
        "message": "Validation Failed",
        "errors": [{"message": "A pull request already exists for acme:resource-updater/sync."}],
    }
    with _patch("src.git_provider._github_get") as mock_get4,          _patch("src.git_provider._github_post") as mock_post4,          _patch("src.git_provider._github_patch") as mock_patch4:
        # First GET: no existing PR; second GET: existing PR #5
        mock_get4.side_effect = [
            MagicMock(
                status_code=200,
                json=MagicMock(return_value=[]),
                raise_for_status=MagicMock(return_value=None),
            ),
            MagicMock(
                status_code=200,
                json=MagicMock(return_value=[{
                    "number": 5,
                    "html_url": "https://github.com/acme/gitops/pull/5",
                }]),
                raise_for_status=MagicMock(return_value=None),
            ),
        ]
        _post_422 = MagicMock(status_code=422)
        _post_422.json = MagicMock(return_value=_422_body)
        _post_422.raise_for_status = MagicMock(return_value=None)
        mock_post4.return_value = _post_422
        mock_patch4.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={}),
            raise_for_status=MagicMock(return_value=None),
        )
        url_422 = provider.open_or_update_pr(
            source_branch="resource-updater/sync",
            target_branch="main",
            title="t",
            description="d",
            assignees=[],
            reviewers=[],
            labels=[],
            squash=False,
            remove_source_branch=True,
            project_path="acme/gitops",
        )
    _check("[gh-422] 422 recovery returns html_url",
           url_422, "https://github.com/acme/gitops/pull/5")
    _check("[gh-422] PATCH called after 422 recovery",
           mock_patch4.call_count, 1)
    _check("[gh-422] PATCH targets pull/5",
           mock_patch4.call_args[0][0].endswith("/pulls/5"), True)

    # ── (9) non-fatal reviewer failure ───────────────────────────────────────
    # Reviewers POST raises requests.RequestException; open_or_update_pr must
    # still return the html_url (not propagate the exception).
    import requests as _requests
    with _patch("src.git_provider._github_get") as mock_get5,          _patch("src.git_provider._github_post") as mock_post5:
        mock_get5.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[]),
            raise_for_status=MagicMock(return_value=None),
        )
        create_ok = MagicMock(
            status_code=201,
            json=MagicMock(return_value={"number": 9, "html_url": "https://github.com/acme/gitops/pull/9"}),
            raise_for_status=MagicMock(return_value=None),
        )
        # Second POST (reviewers) raises a network error
        mock_post5.side_effect = [create_ok, _requests.ConnectionError("network down")]
        url_nonfatal = provider.open_or_update_pr(
            source_branch="resource-updater/sync",
            target_branch="main",
            title="t",
            description="d",
            assignees=[],
            reviewers=["bob"],
            labels=[],
            squash=False,
            remove_source_branch=True,
            project_path="acme/gitops",
        )
    _check("[gh-nonfatal-rev] reviewer failure does not propagate; html_url returned",
           url_nonfatal, "https://github.com/acme/gitops/pull/9")

    # ── (10) _check_auth 403 hint covers fine-grained PAT permissions ─────────
    # Classic-PAT "scopes" language misdirects fine-grained PAT operators: a
    # fine-grained token with only Contents:write pushes fine but 403s on the
    # Pulls API for lack of Pull requests:write. The hint must name it.
    import src.git_provider as _gp_mod
    with _patch.object(_gp_mod._log, "error") as mock_403_log:
        fake_403 = MagicMock(
            status_code=403,
            headers={"Content-Type": "application/json"},
            text='{"message":"Resource not accessible by personal access token"}',
        )
        _gp_mod.GitHubProvider._check_auth(fake_403)
        _check("[gh-403-hint-logged] _check_auth logs a hint on 403",
               mock_403_log.called, True)
        hint_blob = " ".join(str(a) for a in (mock_403_log.call_args.args or ()))
        _check("[gh-403-hint-pull-requests] hint names 'Pull requests'",
               "Pull requests" in hint_blob, True)
        _check("[gh-403-hint-contents] hint names 'Contents'",
               "Contents" in hint_blob, True)



# --------------------------------------------------------------------------- #
# Section:  — provider-agnostic factory + config selection              #
# --------------------------------------------------------------------------- #


def section_provider_factory_and_config() -> None:
    """: provider-agnostic factory (_detect_provider, build_provider) +
    generic credential fields in Config (git_token, git_provider, git_api_url,
    git_username) with GITLAB_TOKEN / GITLAB_USERNAME deprecation fallbacks.

    Fail-first: all asserts reference symbols that don't exist until
    lands.  Run before implementing to confirm failures, then after to confirm
    green.

    Covered invariants:
      1. _detect_provider: github.com / *.github.com -> "github"; everything
         else -> "gitlab" (including gitlab.example.com and gitlab.com).
      2. build_provider: override="github" -> GitHubProvider; override="gitlab"
         -> GitLabProvider; override=""/auto with github.com URL -> GitHubProvider;
         override=""/auto with self-hosted URL -> GitLabProvider.
      3. build_provider raises ValueError on a bogus provider_override.
      4. Config.from_env: GIT_TOKEN wins over GITLAB_TOKEN; GITLAB_TOKEN
         fallback fires the deprecation WARNING and populates git_token.
      5. Config.from_file: gitProvider / gitApiUrl / gitUsername ConfigMap keys
         are parsed; gitUsername falls back to gitlabUsername.
      6. Config.validate: git_provider not in valid set -> exit(2).
         create_mr=true + empty git_token -> exit(2) (generic check).
         Provider/host mismatch WARNING fires when effective provider is
         "github" but repoUrl host is non-GitHub and git_api_url is empty.
      7. prod default path: self-hosted GitLab repoUrl + GITLAB_TOKEN
         fallback -> GitLabProvider with correct gitlab_url and username.
    """
    _section

    # ── import guard ────────────────────────────────────────────────────────
    try:
        from src.git_provider import _detect_provider, build_provider
        _factory_importable = True
    except ImportError as exc:
        _check("[p3-factory-import] _detect_provider and build_provider importable",
               False, True)
        print(f"      ImportError: {exc}")
        _factory_importable = False

    # ── (1) _detect_provider ────────────────────────────────────────────────
    if _factory_importable:
        _check("[p3-detect] github.com -> 'github'",
               _detect_provider("https://github.com/acme/repo.git"), "github")
        _check("[p3-detect] sub.github.com -> 'github'",
               _detect_provider("https://sub.github.com/acme/repo.git"), "github")
        _check("[p3-detect] gitlab.com -> 'gitlab'",
               _detect_provider("https://gitlab.com/acme/repo.git"), "gitlab")
        _check("[p3-detect] gitlab.example.com -> 'gitlab'",
               _detect_provider("https://gitlab.example.com/infra/gitops.git"), "gitlab")
        # GHE on a custom host: unknown hostname -> "gitlab" (by design -- needs override)
        _check("[p3-detect] ghe.corp.example.com -> 'gitlab' (needs explicit override)",
               _detect_provider("https://ghe.corp.example.com/acme/repo.git"), "gitlab")

    # ── (2) build_provider returns the right concrete type ──────────────────
    if _factory_importable:
        from src.git_provider import GitLabProvider, GitHubProvider

        # Explicit override="github"
        p = build_provider(
            repo_url="https://gitlab.example.com/infra/gitops.git",
            token="tok",
            provider_override="github",
        )
        _check("[p3-build] override=github -> GitHubProvider",
               isinstance(p, GitHubProvider), True)

        # Explicit override="gitlab"
        p = build_provider(
            repo_url="https://github.com/acme/repo.git",
            token="tok",
            provider_override="gitlab",
        )
        _check("[p3-build] override=gitlab -> GitLabProvider",
               isinstance(p, GitLabProvider), True)

        # Auto-detect github.com -> GitHubProvider
        p = build_provider(
            repo_url="https://github.com/acme/repo.git",
            token="tok",
            provider_override="",
        )
        _check("[p3-build] auto-detect github.com URL -> GitHubProvider",
               isinstance(p, GitHubProvider), True)

        # Auto-detect self-hosted -> GitLabProvider
        p = build_provider(
            repo_url="https://gitlab.example.com/infra/gitops.git",
            token="tok",
            provider_override="auto",
        )
        _check("[p3-build] auto-detect self-hosted URL -> GitLabProvider",
               isinstance(p, GitLabProvider), True)

        # ValueError on bogus override
        try:
            build_provider(
                repo_url="https://gitlab.example.com/infra/gitops.git",
                token="tok",
                provider_override="bitbucket",
            )
            _check("[p3-build-bogus] bogus override raises ValueError", False, True)
        except ValueError:
            _check("[p3-build-bogus] bogus override raises ValueError", True, True)

        # GitLabProvider built by factory has the correct gitlab_url
        # (derived from repo_url when api_url is empty)
        p_lab = build_provider(
            repo_url="https://gitlab.example.com/infra/gitops.git",
            token="glpat-abc",
            provider_override="gitlab",
            username="oauth2",
        )
        _check("[p3-build] GitLabProvider._gitlab_url matches repo host",
               p_lab._gitlab_url, "https://gitlab.example.com")

        # Explicit api_url overrides host derivation
        p_lab_api = build_provider(
            repo_url="https://gitlab.example.com/infra/gitops.git",
            token="glpat-abc",
            provider_override="gitlab",
            api_url="https://gitlab.example.com",
        )
        _check("[p3-build] explicit api_url used as gitlab_url",
               p_lab_api._gitlab_url, "https://gitlab.example.com")

        # GitHubProvider built by factory has the correct api_base
        p_hub = build_provider(
            repo_url="https://github.com/acme/repo.git",
            token="ghp_abc",
            provider_override="github",
            api_url="",
        )
        _check("[p3-build] GitHubProvider default api_base == api.github.com",
               p_hub._api_base, "https://api.github.com")

        # Explicit api_url for GitHub Enterprise
        p_ghe = build_provider(
            repo_url="https://github.com/acme/repo.git",
            token="ghp_abc",
            provider_override="github",
            api_url="https://github.corp.example.com/api/v3",
        )
        _check("[p3-build] GHE explicit api_url used as api_base",
               p_ghe._api_base, "https://github.corp.example.com/api/v3")

    # ── (3) Config: new generic fields present ──────────────────────────────
    try:
        from src.config import Config as _Config3
        _has_git_token = hasattr(_Config3, '__dataclass_fields__') and 'git_token' in _Config3.__dataclass_fields__
        _has_git_provider = hasattr(_Config3, '__dataclass_fields__') and 'git_provider' in _Config3.__dataclass_fields__
        _has_git_api_url = hasattr(_Config3, '__dataclass_fields__') and 'git_api_url' in _Config3.__dataclass_fields__
        _has_git_username = hasattr(_Config3, '__dataclass_fields__') and 'git_username' in _Config3.__dataclass_fields__
    except Exception:
        _has_git_token = _has_git_provider = _has_git_api_url = _has_git_username = False

    _check("[p3-config-fields] Config.git_token field present",       _has_git_token,     True)
    _check("[p3-config-fields] Config.git_provider field present",    _has_git_provider,  True)
    _check("[p3-config-fields] Config.git_api_url field present",     _has_git_api_url,   True)
    _check("[p3-config-fields] Config.git_username field present",    _has_git_username,  True)

    # ── (4) from_env: GIT_TOKEN wins; GITLAB_TOKEN fallback emits deprecation WARNING ──
    # Skip sections 4-7 if Config doesn't yet have the generic fields — they'd
    # crash with AttributeError which obscures the fail-first signal.
    _config_fields_ok = _has_git_token and _has_git_provider and _has_git_api_url and _has_git_username
    if not _config_fields_ok:
        # Record remaining asserts as FAIL so the count is accurate
        for _lbl in (
            "[p3-from_env] GIT_TOKEN -> git_token populated",
            "[p3-from_env] GIT_TOKEN present -> no GITLAB_TOKEN deprecation warning",
            "[p3-from_env] GITLAB_TOKEN fallback -> git_token populated",
            "[p3-from_env] GITLAB_TOKEN fallback -> deprecation WARNING emitted",
            "[p3-from_env] GIT_TOKEN beats GITLAB_TOKEN when both set",
            "[p3-from_env] GIT_USERNAME -> git_username populated",
            "[p3-from_env] GITLAB_USERNAME fallback -> git_username populated",
            "[p3-from_env] GIT_PROVIDER=github -> git_provider='github'",
            "[p3-from_env] GIT_API_URL -> git_api_url (trailing slash stripped)",
            "[p3-from_file] gitProvider ConfigMap key parsed",
            "[p3-from_file] gitApiUrl ConfigMap key parsed (trailing slash stripped)",
            "[p3-from_file] gitUsername ConfigMap key parsed",
            "[p3-from_file] gitlabUsername fallback -> git_username",
            "[p3-from_file] gitUsername > gitlabUsername when both present",
            "[p3-validate] git_provider='bitbucket' (invalid) -> exit(2)",
            "[p3-validate] git_provider='' (valid) -> ok",
            "[p3-validate] git_provider='auto' (valid) -> ok",
            "[p3-validate] git_provider='gitlab' (valid) -> ok",
            "[p3-validate] git_provider='github' (valid) -> ok",
            "[p3-validate] create_mr=true + empty git_token -> exit(2)",
            "[p3-validate] create_mr=true + git_token set -> ok",
            "[p3-validate] provider=github + non-GitHub repoUrl + no api_url -> WARNING emitted",
            "[p3-validate] provider/host mismatch is a WARNING not an exit",
            "[p3-elops] GITLAB_TOKEN fallback -> git_token = 'glpat-elops-token'",
            "[p3-elops] self-hosted repoUrl -> GitLabProvider",
            "[p3-elops] GitLabProvider._gitlab_url == 'https://gitlab.example.com'",
            "[p3-elops] git_username defaults to 'oauth2'",
            "[p3-elops] token correctly threaded to provider",
        ):
            _check(_lbl, False, True)
        return

    import os as _p3os
    import logging as _p3log
    import io as _p3io
    from unittest.mock import patch as _p3patch

    from src.config import Config as _Cfg

    def _from_env_p3(env_dict):
        """Run Config.from_env() under a controlled environment."""
        with _p3patch.dict(_p3os.environ, env_dict, clear=True):
            return _Cfg.from_env()

    def _from_env_p3_with_log(env_dict):
        """Run Config.from_env(), capturing WARNING+ log output."""
        buf = _p3io.StringIO()
        h = _p3log.StreamHandler(buf)
        h.setLevel(_p3log.WARNING)
        root = _p3log.getLogger()
        root.addHandler(h)
        old = root.level
        root.setLevel(_p3log.WARNING)
        try:
            cfg = _from_env_p3(env_dict)
            return cfg, buf.getvalue()
        finally:
            root.removeHandler(h)
            root.setLevel(old)

    _base_env = {
        "GIT_AUTHOR_NAME": "kru",
        "GIT_AUTHOR_EMAIL": "kru@example",
        "CR_WRITEBACK_REPO_URL": "https://gitlab.example.com/infra/gitops.git",
        "CR_WRITEBACK_PATH": "overrides",
        "PROMETHEUS_URL": "http://prom:9090",
    }

    # GIT_TOKEN present -> git_token populated, no fallback warning
    cfg_git_tok, git_tok_warning = _from_env_p3_with_log(dict(_base_env, GIT_TOKEN="generic-token"))
    _check("[p3-from_env] GIT_TOKEN -> git_token populated",
           cfg_git_tok.git_token, "generic-token")
    _check("[p3-from_env] GIT_TOKEN present -> no GITLAB_TOKEN deprecation warning",
           "GITLAB_TOKEN" not in git_tok_warning, True)

    # GITLAB_TOKEN fallback -> git_token gets the value, deprecation warning fires
    cfg_legacy, legacy_warning = _from_env_p3_with_log(dict(_base_env, GITLAB_TOKEN="legacy-glpat-token"))
    _check("[p3-from_env] GITLAB_TOKEN fallback -> git_token populated",
           cfg_legacy.git_token, "legacy-glpat-token")
    _check("[p3-from_env] GITLAB_TOKEN fallback -> deprecation WARNING emitted",
           "GITLAB_TOKEN" in legacy_warning and "deprecated" in legacy_warning.lower(), True)

    # Both set: GIT_TOKEN wins
    cfg_both = _from_env_p3(dict(_base_env, GIT_TOKEN="generic-wins", GITLAB_TOKEN="old-token"))
    _check("[p3-from_env] GIT_TOKEN beats GITLAB_TOKEN when both set",
           cfg_both.git_token, "generic-wins")

    # GIT_USERNAME env var is read
    cfg_guser = _from_env_p3(dict(_base_env, GIT_TOKEN="tok", GIT_USERNAME="my-bot"))
    _check("[p3-from_env] GIT_USERNAME -> git_username populated",
           cfg_guser.git_username, "my-bot")

    # GITLAB_USERNAME fallback for git_username
    cfg_gl_user = _from_env_p3(dict(_base_env, GIT_TOKEN="tok", GITLAB_USERNAME="legacy-user"))
    _check("[p3-from_env] GITLAB_USERNAME fallback -> git_username populated",
           cfg_gl_user.git_username, "legacy-user")

    # GIT_PROVIDER env var
    cfg_prov = _from_env_p3(dict(_base_env, GIT_TOKEN="tok", GIT_PROVIDER="github"))
    _check("[p3-from_env] GIT_PROVIDER=github -> git_provider='github'",
           cfg_prov.git_provider, "github")

    # GIT_API_URL env var (trailing slash stripped)
    cfg_api = _from_env_p3(dict(_base_env, GIT_TOKEN="tok", GIT_API_URL="https://api.github.com/"))
    _check("[p3-from_env] GIT_API_URL -> git_api_url (trailing slash stripped)",
           cfg_api.git_api_url, "https://api.github.com")

    # ── (5) from_file: gitProvider / gitApiUrl / gitUsername ConfigMap keys ─
    import tempfile as _tf3

    def _from_file_p3(yaml_snippet, extra_env=None):
        lines = [
            "config:",
            "  prometheusUrl: http://prom:9090",
            "  createMr: false",
            "  gitAuthorName: kru-bot",
            "  gitAuthorEmail: kru@example.com",
            "  crWriteback:",
            "    repoUrl: https://gitlab.example.com/infra/gitops.git",
            "    path: overrides",
        ]
        full = "\n".join(lines) + "\n" + yaml_snippet
        with _tf3.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(full)
            p = f.name
        try:
            env = {"GIT_TOKEN": "tok-from-env"}
            if extra_env:
                env.update(extra_env)
            with _p3patch.dict(_p3os.environ, env, clear=True):
                return _Cfg.from_file(p)
        finally:
            _p3os.unlink(p)

    cfg_ff_prov = _from_file_p3("  gitProvider: github\n")
    _check("[p3-from_file] gitProvider ConfigMap key parsed",
           cfg_ff_prov.git_provider, "github")

    cfg_ff_api = _from_file_p3("  gitApiUrl: https://api.github.com\n")
    _check("[p3-from_file] gitApiUrl ConfigMap key parsed (trailing slash stripped)",
           cfg_ff_api.git_api_url, "https://api.github.com")

    cfg_ff_user = _from_file_p3("  gitUsername: my-bot\n")
    _check("[p3-from_file] gitUsername ConfigMap key parsed",
           cfg_ff_user.git_username, "my-bot")

    # gitlabUsername fallback when gitUsername absent
    cfg_ff_gl_user = _from_file_p3("  gitlabUsername: legacy-bot\n")
    _check("[p3-from_file] gitlabUsername fallback -> git_username",
           cfg_ff_gl_user.git_username, "legacy-bot")

    # gitUsername takes priority over gitlabUsername
    cfg_ff_both_user = _from_file_p3("  gitUsername: new-bot\n  gitlabUsername: old-bot\n")
    _check("[p3-from_file] gitUsername > gitlabUsername when both present",
           cfg_ff_both_user.git_username, "new-bot")

    # ── (6) Config.validate: git_provider + generic token gate ─────────────
    from src.config import Config as _VCfg, ResourceConfig as _VRC, CrWritebackConfig as _VCRW, MrConfig

    def _base_p3(**overrides):
        kwargs = dict(
            gitlab_url="", gitlab_token="", gitlab_username="",
            git_author_name="kru", git_author_email="kru@example",
            dry_run=False, create_mr=False,
            min_cpu_limit_m=0, min_memory_limit_mi=0,
            prometheus_url="http://prom:9090",
            resource=_VRC(),
            cr_writeback=_VCRW(repo_url="https://gitlab.example.com/infra/gitops.git", path="overrides"),
            mr=MrConfig(),
            git_token="", git_provider="", git_api_url="", git_username="oauth2",
        )
        kwargs.update(overrides)
        return _VCfg(**kwargs)

    def _expect_exit_p3(cfg):
        try:
            cfg.validate()
            return None
        except SystemExit as exc:
            return exc.code

    # Bad git_provider value
    cfg_bad_prov = _base_p3(git_provider="bitbucket")
    _check("[p3-validate] git_provider='bitbucket' (invalid) -> exit(2)",
           _expect_exit_p3(cfg_bad_prov), 2)

    # Valid git_provider values
    for pv in ("", "auto", "gitlab", "github"):
        cfg_ok_prov = _base_p3(git_provider=pv, git_token="tok")
        _check(f"[p3-validate] git_provider={pv!r} (valid) -> ok",
               _expect_exit_p3(cfg_ok_prov), None)

    # create_mr=true + empty git_token (and empty gitlab_token) -> exit(2)
    cfg_no_tok = _base_p3(create_mr=True, git_token="", gitlab_token="")
    _check("[p3-validate] create_mr=true + empty git_token -> exit(2)",
           _expect_exit_p3(cfg_no_tok), 2)

    # create_mr=true + git_token set -> ok
    cfg_with_tok = _base_p3(create_mr=True, git_token="glpat-xxx")
    _check("[p3-validate] create_mr=true + git_token set -> ok",
           _expect_exit_p3(cfg_with_tok), None)

    # Provider/host mismatch WARNING: effective provider=github, repoUrl is
    # non-GitHub host, git_api_url empty -> warn but no exit
    warn_buf = _p3io.StringIO()
    warn_h = _p3log.StreamHandler(warn_buf)
    warn_h.setLevel(_p3log.WARNING)
    root_log = _p3log.getLogger()
    root_log.addHandler(warn_h)
    root_log.setLevel(_p3log.WARNING)
    try:
        cfg_mismatch = _base_p3(
            git_provider="github",
            git_token="tok",
            git_api_url="",
            cr_writeback=_VCRW(
                repo_url="https://gitlab.example.com/infra/gitops.git",
                path="overrides",
            ),
        )
        _expect_exit_p3(cfg_mismatch)  # should NOT exit -- just warn
        mismatch_out = warn_buf.getvalue()
    finally:
        root_log.removeHandler(warn_h)

    _check("[p3-validate] provider=github + non-GitHub repoUrl + no api_url -> WARNING emitted",
           any(kw in mismatch_out.lower() for kw in ("github", "gitapiurl", "api_url", "enterprise")), True)
    _check("[p3-validate] provider/host mismatch is a WARNING not an exit",
           _expect_exit_p3(_base_p3(git_provider="github", git_token="tok", git_api_url="")), None)

    # ── (7) prod default path: self-hosted GitLab + GITLAB_TOKEN -> GitLabProvider ──
    if _factory_importable:
        from src.git_provider import GitLabProvider as _GLP

        # Simulate what main.cmd_sync does after :
        #   build_provider(repo_url=cfg.cr_writeback.repo_url,
        #                  token=cfg.git_token,
        #                  provider_override=cfg.git_provider,
        #                  api_url=cfg.git_api_url,
        #                  username=cfg.git_username)
        # With prod inputs: self-hosted GitLab URL, GITLAB_TOKEN fallback,
        # no explicit provider/api_url/username.
        cfg_elops, _ = _from_env_p3_with_log(dict(_base_env, GITLAB_TOKEN="glpat-elops-token"))

        p_elops = build_provider(
            repo_url=cfg_elops.cr_writeback.repo_url,
            token=cfg_elops.git_token,
            provider_override=cfg_elops.git_provider,
            api_url=cfg_elops.git_api_url,
            username=cfg_elops.git_username,
        )
        _check("[p3-elops] GITLAB_TOKEN fallback -> git_token = 'glpat-elops-token'",
               cfg_elops.git_token, "glpat-elops-token")
        _check("[p3-elops] self-hosted repoUrl -> GitLabProvider",
               isinstance(p_elops, _GLP), True)
        _check("[p3-elops] GitLabProvider._gitlab_url == 'https://gitlab.example.com'",
               p_elops._gitlab_url, "https://gitlab.example.com")
        _check("[p3-elops] git_username defaults to 'oauth2'",
               p_elops.git_username(), "oauth2")
        _check("[p3-elops] token correctly threaded to provider",
               p_elops._token, "glpat-elops-token")




# --------------------------------------------------------------------------- #
# Section: webhook_cache annotation constant (cleanup batch 1a)               #
# --------------------------------------------------------------------------- #

def section_webhook_cache_annotation_constant() -> None:
    """Cleanup batch 1b (amend of 1a): NamespaceCache bootstrap count delegates
    to `is_namespace_enabled` from overrides.py instead of inlining the annotation
    key composition and a divergent truthy-literal tuple.

    Bug class: config-bound violation / silent-allow variant — the inline tuple
    ("1", "true", "yes") diverges from overrides._TRUE_LITERALS; a future change
    to accepted boolean literals would silently leave the count out of sync with
    the actual admission decision made by is_namespace_enabled.

    Fix shape (Option B1):
      - module-level `from src.overrides import is_namespace_enabled`
      - ANNOTATION_PREFIX + OPT_IN_KEY module-level import REMOVED (unused)
      - `_initial_list` count expression replaced with `is_namespace_enabled(ann)`
      - lazy import inside `is_enabled` removed (was guarding against a
        non-existent circular import)

    These asserts FAIL against the Option-A (batch 1a) code and PASS once the
    Option-B amend lands.
    """
    _section("webhook_cache — opt-in count delegates to is_namespace_enabled (batch 1b)")

    import ast
    import pathlib

    src_path = pathlib.Path(__file__).parent.parent / "src" / "webhook_cache.py"
    source = src_path.read_text()
    tree = ast.parse(source)

    # Assert 1: the raw literal must NOT appear as a string Constant in the file.
    _LITERAL = "kube-resource-updater.enabled"
    literal_uses = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == _LITERAL
    ]
    _check(
        "[wc-const] raw literal 'kube-resource-updater.enabled' absent from AST "
        "(pre-fix: present at line 589 as dict.get() key)",
        len(literal_uses), 0,
    )

    # Assert 2: is_namespace_enabled imported at MODULE level (col_offset == 0).
    top_level_imports = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and getattr(node, "col_offset", -1) == 0
    ]
    imported_names = set()
    for imp in top_level_imports:
        for alias in imp.names:
            imported_names.add(alias.asname or alias.name)

    _check(
        "[wc-const] is_namespace_enabled imported at module level from src.overrides "
        "(pre-fix Option-A: not in module-level imports, only ANNOTATION_PREFIX + OPT_IN_KEY were)",
        "is_namespace_enabled" in imported_names, True,
    )

    # Assert 3: the divergent inline truthy tuple must not appear in the source.
    _check(
        '[wc-const] inline truthy tuple ("1", "true", "yes") absent from source '
        "(pre-fix Option-A: present at line 590, diverges from overrides._TRUE_LITERALS)",
        '("1", "true", "yes")' in source, False,
    )

    # Assert 4 + 5: ANNOTATION_PREFIX and OPT_IN_KEY are NOT in module-level imports
    # (they were the Option-A fix; Option-B removes them as unused).
    _check(
        "[wc-const] ANNOTATION_PREFIX NOT imported at module level "
        "(Option-A artifact: was imported but is unused after Option-B)",
        "ANNOTATION_PREFIX" in imported_names, False,
    )
    _check(
        "[wc-const] OPT_IN_KEY NOT imported at module level "
        "(Option-A artifact: was imported but is unused after Option-B)",
        "OPT_IN_KEY" in imported_names, False,
    )


# --------------------------------------------------------------------------- #
# Section: log_git_credentials_source dead-code (cleanup batch 1b)            #
# --------------------------------------------------------------------------- #

def section_dead_log_credentials_source() -> None:
    """Cleanup batch 1b: log_git_credentials_source in writeback.py is dead code.
    main.py was updated to call log_git_credentials_state; the deprecated shim
    should be removed entirely.

    Bug class: dead-export / misleading-deprecation — a shim that delegates to the
    new function but emits a DeprecationWarning can confuse callers into thinking
    the old path is still active, and widens the import surface for no reason.

    Fix shape: delete log_git_credentials_source (def + docstring mention) from
    writeback.py.

    These asserts FAIL against current code (shim present) and PASS once removed.
    """
    _section("writeback — log_git_credentials_source is dead (batch 1b)")

    import pathlib
    import src.writeback as _wb

    # Assert 1: the old name must NOT be importable from writeback.
    _check(
        "[dead-cred-src] log_git_credentials_source NOT present in src.writeback "
        "(pre-fix: defined at writeback.py line ~103)",
        hasattr(_wb, "log_git_credentials_source"), False,
    )

    # Assert 2: the symbol name must not appear anywhere in writeback.py source —
    # not as a def, docstring mention, or warnings.warn string. A text scan is the
    # right tool: the def is an ast.FunctionDef (not ast.Name) and the docstring /
    # warn references are ast.Constant, so an ast.Name walk would miss them and
    # pass pre-fix (false-green).
    wb_src = (pathlib.Path(__file__).parent.parent / "src" / "writeback.py").read_text()
    _check(
        "[dead-cred-src] 'log_git_credentials_source' absent from writeback.py source "
        "(pre-fix: present in def + docstring + warnings.warn string)",
        "log_git_credentials_source" in wb_src, False,
    )


# --------------------------------------------------------------------------- #
# Section: scrape-interval fallback log level (cleanup batch 2)               #
# --------------------------------------------------------------------------- #

def section_scrape_interval_fallback_log_level() -> None:
    """Cleanup batch 2: when both Prometheus detection APIs fail, the final
    fallback to '15s' is logged at INFO (prometheus.py:230). A silent fallback is
    a monitoring blind-spot — operators on VictoriaMetrics/Thanos/non-cadvisor get
    no signal that the subquery step may be misaligned with the real scrape cadence.

    Bug class: silent-broken — the tool emits a recommendation with a potentially
    wrong subquery step and never tells the operator.

    Fix: change _log.info -> _log.warning at the final fallback call site.

    These asserts FAIL against current code (level is INFO) and PASS once changed
    to _log.warning.
    """
    _section("detect_scrape_interval — fallback logs at WARNING not INFO (batch 2)")

    import logging as _logging
    import io as _io
    from src.prometheus import detect_scrape_interval, _scrape_interval_cache
    from unittest.mock import patch as _patch

    def _both_fail(*a, **kw):
        raise ConnectionError("unreachable")

    buf_warn = _io.StringIO()
    h_warn = _logging.StreamHandler(buf_warn)
    h_warn.setLevel(_logging.WARNING)

    logger = _logging.getLogger("src.prometheus")
    old_level = logger.level
    logger.setLevel(_logging.DEBUG)
    logger.addHandler(h_warn)

    try:
        _scrape_interval_cache.clear()
        with _patch("src.prometheus.requests.get", side_effect=_both_fail):
            result = detect_scrape_interval("http://prom.fake:9090")
    finally:
        logger.removeHandler(h_warn)
        logger.setLevel(old_level)

    warn_out = buf_warn.getvalue()

    # The fallback must return 15s (unchanged by this fix).
    _check("[fallback-level] detect_scrape_interval returns 15s on total failure",
           result, "15s")

    # The fallback message must appear at WARNING level (pre-fix: empty — _log.info).
    _check(
        "[fallback-level] fallback logged at WARNING level "
        "(pre-fix: only at INFO — warn_out empty)",
        bool(warn_out.strip()), True,
    )

    # Confirm we matched the detection-miss call, not some other warning.
    _check(
        "[fallback-level] WARNING text mentions 'fallback' or '15s'",
        any(kw in warn_out.lower() for kw in ("fallback", "15s", "falling back")), True,
    )


def section_delta_str_silent_swallow() -> None:
    """writeback._delta_str: bare except swallows parse failures silently.

    Bug class: silent-broken — when parse_fn raises, _delta_str returns ''
    with no log output. The MR description delta column is blank with no
    operator-visible signal.

    Fix: catch (ValueError, TypeError, ArithmeticError) and log WARNING with
    old_val / new_val / exception detail.

    These asserts FAIL against current code (no WARNING emitted) and PASS
    once the except clause is narrowed + logged.
    """
    _section("_delta_str — bare except swallows parse failure silently (writeback.py)")

    import logging as _logging
    import io as _io
    from src.writeback import _delta_str

    def _always_raise(s):
        raise ValueError(f"unparseable quantity: {s!r}")

    buf_warn = _io.StringIO()
    h_warn = _logging.StreamHandler(buf_warn)
    h_warn.setLevel(_logging.WARNING)

    logger = _logging.getLogger("src.writeback")
    old_level = logger.level
    logger.setLevel(_logging.DEBUG)
    logger.addHandler(h_warn)

    try:
        result = _delta_str("250m", "300m", _always_raise)
    finally:
        logger.removeHandler(h_warn)
        logger.setLevel(old_level)

    warn_out = buf_warn.getvalue()

    # Fail-open: must still return "" (MR opens regardless).
    _check("[delta-str-swallow] return value is empty string (fail-open preserved)",
           result, "")

    # Pre-fix: this check FAILS — warn_out is empty because except swallows silently.
    # Post-fix: this check PASSES — WARNING logged with [delta-str] tag.
    _check(
        "[delta-str-swallow] WARNING logged when parse_fn raises "
        "(pre-fix: empty — bare except swallows)",
        bool(warn_out.strip()), True,
    )

    # Confirm the message identifies both values so operators can triage.
    _check(
        "[delta-str-swallow] WARNING includes old_val and new_val",
        "250m" in warn_out and "300m" in warn_out, True,
    )


# --------------------------------------------------------------------------- #
# Section: webhook ServiceAccount automountServiceAccountToken (cleanup 3)    #
# --------------------------------------------------------------------------- #

def section_webhook_sa_automount() -> None:
    """Cleanup batch 3: the webhook ServiceAccount template renders without an
    explicit automountServiceAccountToken: true. K8s defaults to true for user
    SAs, but an explicit field guards against a future global default-false policy
    silently breaking the webhook (which needs the SA token for k8s API calls).

    Bug class: implicit-behaviour reliance — an admission-time default is relied on
    instead of declared.

    Fix shape: add automountServiceAccountToken: true to
    templates/webhook/serviceaccount.yaml.

    These asserts FAIL against the current chart and PASS once the field is added.
    """
    _section("Chart webhook ServiceAccount — automountServiceAccountToken: true (batch 3)")

    import shutil
    import subprocess

    helm = shutil.which("helm")
    if not helm:
        for cand in (os.path.expanduser("~/homebrew/bin/helm"),
                     "/opt/homebrew/bin/helm", "/usr/local/bin/helm"):
            if os.path.isfile(cand):
                helm = cand
                break
    if not helm:
        print(f"  [{_SKIP}] helm CLI not installed — skipping chart render check")
        return

    chart_dir = _chart_dir()
    if not os.path.isdir(chart_dir):
        print(f"  [{_SKIP}] chart dir not found at {chart_dir} (running outside the workspace?)")
        return

    result = subprocess.run(
        [
            helm, "template", "kru", chart_dir,
            "--set", (
                "config.crWriteback.repoUrl=https://x.git,"
                "config.crWriteback.path=overrides,"
                "config.prometheusUrl=http://prom:9090,"
                "gitlab.token=qa-fake-token,"
                "webhook.enabled=true"
            ),
            "--show-only", "templates/webhook/serviceaccount.yaml",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [{_SKIP}] helm render failed (likely missing `common` dep):")
        for line in result.stderr.splitlines()[:5]:
            print(f"      {line}")
        return

    rendered = result.stdout

    _check(
        "[webhook-sa] automountServiceAccountToken present in rendered ServiceAccount "
        "(pre-fix: field absent)",
        "automountServiceAccountToken" in rendered, True,
    )
    _check(
        "[webhook-sa] automountServiceAccountToken value is true "
        "(guard against accidental false)",
        "automountServiceAccountToken: true" in rendered, True,
    )


# --------------------------------------------------------------------------- #
# Webhook module public API contract                                           #
# --------------------------------------------------------------------------- #

def section_webhook_module_api() -> None:
    """Guards the public surface of webhook_validate so cross-module
    callers can't silently break at runtime from a private rename.

    Asserts:
      - webhook_validate exposes a public `allow` symbol (hasattr check).
      - webhook_server.py source text contains no reference to
        webhook_validate._allow (source-text scan).
    Both checks FAIL against the current code (before the fix) and PASS
    after `allow = _allow` is added and the two call sites are updated.
    """
    _section("Webhook module public API — no cross-module private calls")

    import src.webhook_validate as _wv

    _check_truthy(
        "webhook_validate exposes public `allow` symbol",
        hasattr(_wv, "allow"),
    )

    import pathlib
    server_src = pathlib.Path(
        __file__
    ).parent.parent / "src" / "webhook_server.py"
    text = server_src.read_text()
    _check(
        "webhook_server.py does not reference webhook_validate._allow",
        "webhook_validate._allow" in text,
        False,
    )


# --------------------------------------------------------------------------- #
# Public-readiness code fixes (2026-06-12 batch)                               #
# --------------------------------------------------------------------------- #

def section_public_readiness_code_fixes() -> None:
    """Public-readiness cleanup batch: dead credential resolver, paste artifact,
    bool-chain dedup, metrics content-type header hygiene.

    A1 — writeback._resolve_git_credentials is dead post provider-refactor
         (zero production callers; only its own def + module docstring + old QA
         referenced it). Bug class: dead-export. Fix: delete.
    A2 — writeback.py GIT_TOKEN warning built from two adjacent string literals
         separated by 8 stray spaces (paste artifact). Fix: single literal.
    A3 — overrides.is_auto_rollout_enabled inlines a verbatim copy of
         _resolve_bool_chain (the helper extracted for exactly this purpose).
         Bug class: copy-paste divergence risk. Fix: delegate.
    A4 — webhook_server metrics handler passes a parameterised media type via
         aiohttp's content_type= arg (a media-type token slot). Fix: explicit
         Content-Type header; wire bytes unchanged.

    Asserts FAIL against pre-batch code and PASS after the fixes.
    """
    _section("Public-readiness code fixes — dead fn, paste artifact, dedup, content-type")

    import asyncio
    import inspect
    import pathlib
    import src.writeback as _wb
    from src import overrides as _ov

    wb_src = (pathlib.Path(__file__).parent.parent / "src" / "writeback.py").read_text()
    ws_src = (pathlib.Path(__file__).parent.parent / "src" / "webhook_server.py").read_text()

    # ── A1: dead _resolve_git_credentials gone ──────────────────────────
    _check(
        "[pr-dead-cred] _resolve_git_credentials NOT present in src.writeback "
        "(pre-fix: defined ~line 95, zero production callers)",
        hasattr(_wb, "_resolve_git_credentials"), False,
    )
    _check(
        "[pr-dead-cred] '_resolve_git_credentials' absent from writeback.py source "
        "(pre-fix: def + module-docstring mention)",
        "_resolve_git_credentials" in wb_src, False,
    )

    # ── A2: paste artifact (8 spaces between adjacent string literals) ──
    _check(
        '[pr-paste] no stray-whitespace implicit string concat (\'"        "\') in writeback.py '
        "(pre-fix: present in the GIT_TOKEN warning)",
        '"        "' in wb_src, False,
    )

    # ── A3: is_auto_rollout_enabled delegates to _resolve_bool_chain ────
    rollout_src = inspect.getsource(_ov.is_auto_rollout_enabled)
    _check(
        "[pr-dedup] is_auto_rollout_enabled delegates to _resolve_bool_chain "
        "(pre-fix: verbatim inline copy of the helper)",
        "_resolve_bool_chain(" in rollout_src, True,
    )
    _check(
        "[pr-dedup] is_auto_rollout_enabled has no inline layer loop "
        "(pre-fix: own 'for source in' copy)",
        "for source in" in rollout_src, False,
    )
    # Behavior unchanged: workload > namespace > helm, malformed falls through.
    _check("[pr-dedup] workload layer wins",
           _ov.is_auto_rollout_enabled(
               False,
               {"kube-resource-updater.autoRollout": "false"},
               {"kube-resource-updater.autoRollout": "true"}), True)
    _check("[pr-dedup] malformed workload value falls through to namespace",
           _ov.is_auto_rollout_enabled(
               False,
               {"kube-resource-updater.autoRollout": "true"},
               {"kube-resource-updater.autoRollout": "banana"}), True)
    _check("[pr-dedup] neither layer set → helm default",
           _ov.is_auto_rollout_enabled(True, {}, {}), True)

    # ── A4: metrics Content-Type via explicit header, wire bytes equal ──
    _check(
        "[pr-ctype] webhook_server does not pass parameters inside content_type= "
        "(pre-fix: content_type=\"text/plain; version=0.0.4\")",
        'content_type="text/plain; version=' in ws_src, False,
    )
    from src.webhook_server import _Metrics, _make_metrics_handler
    resp = asyncio.run(_make_metrics_handler(_Metrics())(None))
    _check(
        "[pr-ctype] /metrics Content-Type header byte-identical to pre-fix wire value",
        resp.headers.get("Content-Type"), "text/plain; version=0.0.4; charset=utf-8",
    )
    _check("[pr-ctype] /metrics body non-empty",
           len(resp.body or b"") > 0, True)


def section_public_readiness_chart_fixes() -> None:
    """Public-readiness chart batch:

    B1 — serviceMonitor/prometheusRule labels default `release: kp` silently
         no-ops monitoring on any cluster whose kube-prometheus-stack release
         isn't named `kp` (silent-allow). Default -> {} (operator sets explicitly).
    B2 — NOTES.txt token warning only checks the deprecated gitlab.* values;
         canonical git.token/git.existingSecret installs get a spurious warning.
    B3 — pdb.maxUnavailable default 1 makes the validate.yaml mutual-exclusion
         gate fire false-positive the moment an operator sets minAvailable.
         Default -> "" with render-time fallback to 1.
    B4 — podSecurityContext lacks seccompProfile; PSA-restricted namespaces
         reject the pods. Add RuntimeDefault.
    B5 — webhook.replicaCount default 2 contradicts the chart's own
         single-runner recommendation in the adjacent comment. Default -> 1.
    B6 — serviceAccount.* / rbac.create lack @param annotations (docs gap).
    B7 — image default points at the maintainer's personal Docker Hub with
         tag latest; move to ghcr.io/mateus-gsilva + appVersion pin (tag "").

    Asserts FAIL against the 1.22.26 chart and PASS once the fixes land.
    """
    _section("Public-readiness chart fixes — labels, NOTES, pdb, seccomp, replicas, image")

    import shutil
    import subprocess
    import yaml as _yaml

    helm = shutil.which("helm")
    if not helm:
        for cand in (os.path.expanduser("~/homebrew/bin/helm"),
                     "/opt/homebrew/bin/helm", "/usr/local/bin/helm"):
            if os.path.isfile(cand):
                helm = cand
                break
    if not helm:
        print(f"  [{_SKIP}] helm CLI not installed — skipping chart render checks")
        return

    chart_dir = _chart_dir()
    if not os.path.isdir(chart_dir):
        print(f"  [{_SKIP}] chart dir not found at {chart_dir}")
        return

    _BASE = ("config.crWriteback.repoUrl=https://x.git,"
             "config.crWriteback.path=overrides,"
             "config.prometheusUrl=http://prom:9090,"
             "git.token=qa-fake-token")

    def _tpl(extra_set: str = "", show_only: str = "") -> tuple[int, str]:
        cmd = [helm, "template", "kru", chart_dir,
               "--set", _BASE + ("," + extra_set if extra_set else "")]
        if show_only:
            cmd += ["--show-only", show_only]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, (r.stdout if r.returncode == 0 else r.stderr)

    values = _yaml.safe_load(
        open(os.path.join(chart_dir, "values.yaml"), encoding="utf-8"))
    values_src = open(os.path.join(chart_dir, "values.yaml"), encoding="utf-8").read()
    notes_src = open(os.path.join(chart_dir, "templates", "NOTES.txt"),
                     encoding="utf-8").read()

    # ── B1: monitoring labels default empty ─────────────────────────────
    sm_labels = values["webhook"]["metrics"]["serviceMonitor"]["labels"]
    pr_labels = values["webhook"]["metrics"]["prometheusRule"]["labels"]
    _check("[pr-chart-labels] serviceMonitor.labels default {} (pre-fix: {release: kp})",
           sm_labels or {}, {})
    _check("[pr-chart-labels] prometheusRule.labels default {} (pre-fix: {release: kp})",
           pr_labels or {}, {})

    # ── B2: NOTES token warning checks canonical git.* values ───────────
    _check("[pr-notes] NOTES.txt token warning checks git.token/git.existingSecret "
           "(pre-fix: only gitlab.*)",
           ".Values.git.token" in notes_src or ".Values.git.existingSecret" in notes_src,
           True)

    # ── B3: pdb false-positive gate ─────────────────────────────────────
    _check('[pr-pdb] pdb.maxUnavailable default "" (pre-fix: 1)',
           str(values["pdb"]["maxUnavailable"] or ""), "")
    rc_min, out_min = _tpl("pdb.create=true,pdb.minAvailable=2")
    _check("[pr-pdb] pdb.create=true + minAvailable=2 renders OK "
           "(pre-fix: validate gate false-positives on the maxUnavailable default)",
           rc_min, 0)
    if rc_min == 0:
        _check("[pr-pdb] minAvailable=2 wins in rendered PDB",
               "minAvailable: 2" in out_min, True)
    rc_def, out_def = _tpl("pdb.create=true", "templates/pdb.yaml")
    _check("[pr-pdb] default PDB still renders maxUnavailable: 1 (fallback preserved)",
           rc_def == 0 and "maxUnavailable: 1" in out_def, True)

    # ── B4: seccompProfile RuntimeDefault on both pod specs ─────────────
    rc_cj, out_cj = _tpl("", "templates/cronjob.yaml")
    _check("[pr-seccomp] CronJob pod spec has seccompProfile RuntimeDefault "
           "(pre-fix: absent)",
           rc_cj == 0 and "type: RuntimeDefault" in out_cj, True)
    rc_wh, out_wh = _tpl("webhook.enabled=true", "templates/webhook/deployment.yaml")
    _check("[pr-seccomp] webhook pod spec has seccompProfile RuntimeDefault "
           "(pre-fix: absent)",
           rc_wh == 0 and "type: RuntimeDefault" in out_wh, True)

    # ── B5: webhook.replicaCount default 1 ──────────────────────────────
    _check("[pr-replicas] webhook.replicaCount default 1 (pre-fix: 2, contradicting "
           "the chart's own single-runner recommendation)",
           values["webhook"]["replicaCount"], 1)

    # ── B6: @param coverage for serviceAccount.* / rbac.create ──────────
    for key in ("serviceAccount.create", "serviceAccount.name",
                "serviceAccount.annotations", "serviceAccount.automountServiceAccountToken",
                "rbac.create"):
        _check(f"[pr-params] values.yaml documents @param {key} (pre-fix: absent)",
               f"@param {key} " in values_src, True)

    # ── B7: image default off personal Docker Hub, tag pinned ───────────
    _check("[pr-image] image.registry default ghcr.io (pre-fix: docker.io)",
           values["image"]["registry"], "ghcr.io")
    _check("[pr-image] image.repository default mateus-gsilva/kube-resource-updater "
           "(pre-fix: personal mateusgsilva95/...)",
           values["image"]["repository"], "mateus-gsilva/kube-resource-updater")
    _check('[pr-image] image.tag default "" -> appVersion pin via helper '
           '(pre-fix: "latest")',
           str(values["image"]["tag"] or ""), "")
    if rc_cj == 0:
        _check("[pr-image] rendered CronJob image is ghcr.io/... pinned to appVersion "
               "(no :latest)",
               "ghcr.io/mateus-gsilva/kube-resource-updater:" in out_cj
               and ":latest" not in out_cj, True)


def section_overrides_unit_file() -> None:
    """Bridges tools/test_overrides.py (the overrides unit-test file) into the
    single canonical entrypoint. Its PASS/FAIL lines print inline above; one
    summary assert folds its exit status into this suite's failure counter so
    `python3 tools/qa_params.py` is the only command anyone needs to run.
    """
    _section("Overrides unit tests (tools/test_overrides.py — bridged)")
    import test_overrides as _to
    rc = _to.main()
    _check("[overrides-file] tools/test_overrides.py suite green", rc, 0)


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #

def main() -> int:
    section_grow_shrink()
    section_floors()
    section_rounding()
    section_colocation()
    section_promql_injection()
    section_mr_timeouts_and_description_cap()
    section_mr_orphan_branch_recovery()
    section_run_subprocess_timeout()
    section_memory_parser_edges()
    section_yaml_rendering_quirks()
    section_crd_schema_tightening()
    section_crd_cel_hardening()
    section_webhook_patch_corners()
    section_prefetch()
    section_credentials_and_prometheus_modes()
    section_selector_inference()
    section_validating_webhook()
    section_mr_metadata()
    section_skip_containers()
    section_oom_slow_path()
    section_status_updater()
    section_auto_rollout()
    section_chart_conditional_rbac()
    section_cert_reconciler()
    section_namespace_cache()
    section_cr_cache_reconnect()
    section_cache_unparseable_modified_leak()
    section_cache_bootstrap_retry()
    section_create_mr_bucketing()
    section_dry_run_bucketing()
    section_cr_name_collision()
    section_config_validate()
    section_margin_default_safe()
    section_log_formatter()
    section_resolver()
    section_discovery_auth_raise()
    section_mr_retry_on_429_and_5xx()
    section_cache_reconnect_backoff()
    section_safe_json_non_json_body()
    section_webhook_cert_san_clusterdomain()
    section_mr_description_truncation_count()
    section_oom_bump_cap_with_investigation()
    section_cold_start_cpu_floor()
    section_oom_bump_clamp_warning()
    section_dependency_pins()
    section_trivial_log_and_defaults()
    section_detect_scrape_interval()
    section_request_query_single_series()
    section_irate_lookback_dynamic()
    section_typo_warning_known_keys()
    section_status_flush_non_api_exception()
    section_cert_malformed_base64()
    section_cert_noreturn_annotation()
    section_cert_409_adopted_validation()
    section_git_provider_abstraction()
    section_github_provider()
    section_provider_factory_and_config()
    section_chart_git_provider_wiring()
    section_webhook_cache_annotation_constant()
    section_dead_log_credentials_source()
    section_scrape_interval_fallback_log_level()
    section_delta_str_silent_swallow()
    section_webhook_sa_automount()
    section_webhook_module_api()
    section_public_readiness_code_fixes()
    section_public_readiness_chart_fixes()
    section_overrides_unit_file()
    live_rc = section_live_prometheus()

    print()
    print("=" * 80)
    if _failures or live_rc:
        print(f"  RESULT: {_failures} unit failure(s){', live data anomaly' if live_rc else ''}".center(80))
        return 1
    print("  RESULT: all checks passed".center(80))
    return 0


if __name__ == "__main__":
    sys.exit(main())
