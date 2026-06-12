"""
Unit tests for src.overrides.

Covers:
  - parse_annotations: prefix filter, type coercion, scope validation,
    bad-value tolerance, unknown-key warnings.
  - merge: layer precedence (workload > ns).
  - apply: dataclass replacement, no-op fast paths, target routing
    (Config vs ResourceConfig fields).
  - is_namespace_enabled / is_workload_skipped: opt-in / skip helpers.
  - resolve_for_workload: full helm → ns → workload chain.

Run via the canonical suite (`python3 tools/qa_params.py` — bridged in as
a section) or standalone: `python3 tools/test_overrides.py`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.config import Config, CrWritebackConfig, ResourceConfig
from src.overrides import (
    ANNOTATION_PREFIX,
    KNOWN_KEYS,
    apply,
    is_namespace_enabled,
    is_workload_skipped,
    merge,
    parse_annotations,
    resolve_for_workload,
)

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"

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


def _check_true(label: str, got) -> None:
    _check(label, bool(got), True)


def _check_false(label: str, got) -> None:
    _check(label, bool(got), False)


# Build a Config the same way Config.from_file() would, but without I/O.
def _base_config() -> Config:
    return Config(
        gitlab_url="",
        gitlab_token="",
        gitlab_username="",
        git_author_name="aru",
        git_author_email="aru@example",
        dry_run=False,
        create_mr=True,
        min_cpu_limit_m=0,
        min_memory_limit_mi=0,
        prometheus_url="",
        resource=ResourceConfig(),
        cr_writeback=CrWritebackConfig(repo_url="https://x", path="overrides"),
        grow_only=False,
        shrink_only=False,
    )


# --------------------------------------------------------------------------- #
# parse_annotations                                                            #
# --------------------------------------------------------------------------- #

def test_parse_basic_typing() -> None:
    print("test_parse_basic_typing:")
    raw = {
        "kube-resource-updater.growOnly": "true",
        "kube-resource-updater.cpuPercentile": "0.95",
        "kube-resource-updater.minCpuRequestM": "100",
        "kube-resource-updater.cpuRequestWindow": "5d",
    }
    out = parse_annotations(raw, scope="namespace")
    _check("growOnly parsed as True",     out.get("growOnly"),         True)
    _check("cpuPercentile parsed as float", out.get("cpuPercentile"),  0.95)
    _check("minCpuRequestM parsed as int",  out.get("minCpuRequestM"), 100)
    _check("cpuRequestWindow stays str",    out.get("cpuRequestWindow"), "5d")


def test_parse_filters_prefix() -> None:
    print("test_parse_filters_prefix:")
    raw = {
        "kube-resource-updater.growOnly": "true",
        "kubernetes.io/created-by": "operator",
        "goldilocks.fairwinds.com/enabled": "true",
        "app.kubernetes.io/instance": "n8n",
    }
    out = parse_annotations(raw, scope="namespace")
    _check("only kube-resource-updater keys returned",
           sorted(out.keys()), ["growOnly"])


def test_parse_unknown_key_dropped() -> None:
    print("test_parse_unknown_key_dropped:")
    raw = {
        "kube-resource-updater.cpuPercentile": "0.9",
        "kube-resource-updater.fooBar": "anything",     # typo / not in spec
    }
    out = parse_annotations(raw, scope="workload")
    _check("known key kept",  out.get("cpuPercentile"), 0.9)
    _check("unknown dropped", "fooBar" in out, False)


def test_parse_bad_value_dropped() -> None:
    print("test_parse_bad_value_dropped:")
    raw = {
        "kube-resource-updater.cpuPercentile": "not-a-number",
        "kube-resource-updater.growOnly": "maybe",
        "kube-resource-updater.minCpuRequestM": "100",
    }
    out = parse_annotations(raw, scope="namespace")
    _check("bad float dropped",   "cpuPercentile" in out, False)
    _check("bad bool dropped",    "growOnly" in out,      False)
    _check("good int kept",        out.get("minCpuRequestM"), 100)


def test_parse_scope_enabled() -> None:
    print("test_parse_scope_enabled:")
    on_ns = parse_annotations(
        {"kube-resource-updater.enabled": "true"}, scope="namespace",
    )
    on_wl = parse_annotations(
        {"kube-resource-updater.enabled": "true"}, scope="workload",
    )
    _check("enabled accepted on namespace", on_ns.get("enabled"), True)
    _check("enabled rejected on workload", "enabled" in on_wl,    False)


def test_parse_scope_skip() -> None:
    print("test_parse_scope_skip:")
    on_ns = parse_annotations(
        {"kube-resource-updater.skip": "true"}, scope="namespace",
    )
    on_wl = parse_annotations(
        {"kube-resource-updater.skip": "true"}, scope="workload",
    )
    _check("skip rejected on namespace", "skip" in on_ns, False)
    _check("skip accepted on workload",  on_wl.get("skip"), True)


def test_parse_empty_input() -> None:
    print("test_parse_empty_input:")
    _check("None input → empty", parse_annotations(None, scope="namespace"), {})
    _check("{} input → empty",   parse_annotations({},   scope="workload"),  {})


# --------------------------------------------------------------------------- #
# merge                                                                        #
# --------------------------------------------------------------------------- #

def test_merge_workload_overrides_ns() -> None:
    print("test_merge_workload_overrides_ns:")
    ns = {"growOnly": True, "cpuPercentile": 0.90}
    wl = {"growOnly": False, "marginFraction": 0.20}
    merged = merge(ns, wl)
    _check("workload growOnly wins",   merged.get("growOnly"),       False)
    _check("ns cpuPercentile kept",    merged.get("cpuPercentile"),  0.90)
    _check("workload-only key kept",   merged.get("marginFraction"), 0.20)


def test_merge_handles_none_layers() -> None:
    print("test_merge_handles_none_layers:")
    merged = merge(None, {"growOnly": True}, None)
    _check("None layers ignored", merged, {"growOnly": True})


# --------------------------------------------------------------------------- #
# apply                                                                        #
# --------------------------------------------------------------------------- #

def test_apply_routes_to_correct_dataclass() -> None:
    print("test_apply_routes_to_correct_dataclass:")
    base = _base_config()
    out = apply(base, {
        "growOnly": True,           # → Config
        "cpuPercentile": 0.95,      # → ResourceConfig
        "minCpuLimitM": 50,         # → Config
        "marginFraction": 0.10,     # → ResourceConfig
    })
    _check("growOnly written to Config",       out.grow_only,                  True)
    _check("minCpuLimitM written to Config",   out.min_cpu_limit_m,            50)
    _check("cpuPercentile in ResourceConfig",  out.resource.cpu_percentile,    0.95)
    _check("marginFraction in ResourceConfig", out.resource.margin_fraction,   0.10)
    # Untouched fields keep their base values.
    _check("base.shrink_only untouched",       out.shrink_only,                False)
    _check("base.create_mr untouched",         out.create_mr,                  True)


def test_apply_no_op_returns_same_object() -> None:
    print("test_apply_no_op_returns_same_object:")
    base = _base_config()
    same = apply(base, {})
    _check("empty overrides → identity", same is base, True)
    same2 = apply(base, {"enabled": True, "skip": True})  # filtered out
    _check("only-marker overrides → identity", same2 is base, True)


def test_apply_skips_marker_keys() -> None:
    print("test_apply_skips_marker_keys:")
    base = _base_config()
    out = apply(base, {"enabled": True, "skip": True, "growOnly": True})
    _check("growOnly applied",   out.grow_only,    True)
    # `enabled` and `skip` aren't dataclass fields → must not have raised.
    _check("config still typed", isinstance(out, Config), True)


# --------------------------------------------------------------------------- #
# is_namespace_enabled / is_workload_skipped                                   #
# --------------------------------------------------------------------------- #

def test_namespace_enabled_helpers() -> None:
    print("test_namespace_enabled_helpers:")
    _check_true ("annot true",   is_namespace_enabled({ANNOTATION_PREFIX + "enabled": "true"}))
    _check_true ("annot 1",      is_namespace_enabled({ANNOTATION_PREFIX + "enabled": "1"}))
    _check_true ("annot yes",    is_namespace_enabled({ANNOTATION_PREFIX + "enabled": "yes"}))
    _check_false("annot false",  is_namespace_enabled({ANNOTATION_PREFIX + "enabled": "false"}))
    _check_false("annot empty",  is_namespace_enabled({ANNOTATION_PREFIX + "enabled": ""}))
    _check_false("missing annot", is_namespace_enabled({}))
    _check_false("None input",    is_namespace_enabled(None))
    _check_false("garbage value", is_namespace_enabled({ANNOTATION_PREFIX + "enabled": "maybe"}))


def test_workload_skip_helpers() -> None:
    print("test_workload_skip_helpers:")
    _check_true ("annot true",   is_workload_skipped({ANNOTATION_PREFIX + "skip": "true"}))
    _check_false("missing",      is_workload_skipped({}))
    _check_false("annot false",  is_workload_skipped({ANNOTATION_PREFIX + "skip": "false"}))


# --------------------------------------------------------------------------- #
# resolve_for_workload                                                         #
# --------------------------------------------------------------------------- #

def test_resolve_full_chain() -> None:
    print("test_resolve_full_chain:")
    base = _base_config()
    ns = {
        "kube-resource-updater.shrinkOnly": "true",
        "kube-resource-updater.cpuPercentile": "0.95",
        "kube-resource-updater.marginFraction": "0.10",
    }
    wl = {
        # Workload flips back to growOnly and bumps the margin
        "kube-resource-updater.growOnly": "true",
        "kube-resource-updater.shrinkOnly": "false",
        "kube-resource-updater.marginFraction": "0.30",
    }
    eff = resolve_for_workload(base, ns, wl, namespace_name="n8n", workload_name="n8n-worker")
    _check("workload growOnly wins",        eff.grow_only,                  True)
    _check("workload shrinkOnly wins",      eff.shrink_only,                False)
    _check("ns cpuPercentile kept",         eff.resource.cpu_percentile,    0.95)
    _check("workload margin kept",          eff.resource.margin_fraction,   0.30)


def test_resolve_no_overrides() -> None:
    print("test_resolve_no_overrides:")
    base = _base_config()
    eff = resolve_for_workload(base, None, None)
    _check("identity when both empty", eff is base, True)


# --------------------------------------------------------------------------- #
# KNOWN_KEYS sanity                                                            #
# --------------------------------------------------------------------------- #

def test_known_keys_includes_markers() -> None:
    print("test_known_keys_includes_markers:")
    _check("'enabled' in KNOWN_KEYS", "enabled" in KNOWN_KEYS, True)
    _check("'skip' in KNOWN_KEYS",    "skip"    in KNOWN_KEYS, True)
    _check("'growOnly' in KNOWN_KEYS","growOnly" in KNOWN_KEYS, True)
    _check("typo NOT in KNOWN_KEYS",  "fooBar"  in KNOWN_KEYS, False)


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [
        test_parse_basic_typing,
        test_parse_filters_prefix,
        test_parse_unknown_key_dropped,
        test_parse_bad_value_dropped,
        test_parse_scope_enabled,
        test_parse_scope_skip,
        test_parse_empty_input,
        test_merge_workload_overrides_ns,
        test_merge_handles_none_layers,
        test_apply_routes_to_correct_dataclass,
        test_apply_no_op_returns_same_object,
        test_apply_skips_marker_keys,
        test_namespace_enabled_helpers,
        test_workload_skip_helpers,
        test_resolve_full_chain,
        test_resolve_no_overrides,
        test_known_keys_includes_markers,
    ]
    for t in tests:
        t()
        print()

    print(f"Total failures: {_failures}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
