"""Prometheus helpers for resource limit and request estimation.

Single-URL model: every query takes the cluster's Prometheus URL directly
(passed in by the caller from `Config.prometheus_url`). The previous
multi-URL `dict[cluster, url]` plumbing was deleted along with multi-cluster
fan-out — the tool now runs one chart release per cluster, so a single URL
is the only thing that ever existed in practice.
"""

import re
import requests

from . import log as _log_module

_log = _log_module.get(__name__)


# PromQL label-value string escape (RFC: https://prometheus.io/docs/prometheus/latest/querying/basics/#string-literals).
# Used for the `=` matcher path: `label="literal"`. Backslash and double-quote
# need escaping; newline becomes `\n`. Without this, a workload `target_name`
# containing `.` would be interpreted by the RE2 regex engine as "any char"
# inside `pod=~"<name>-.*"`, cross-matching unintended pods.
def _escape_label_value(s: str) -> str:
    """Escape a string for use inside a PromQL `label="<here>"` matcher.

    Handles the three PromQL string-literal special chars: backslash,
    double-quote, newline. K8s namespace/container/workload names don't
    normally contain these (alphanumeric + `-` + `.` per RFC 1123), but
    defense-in-depth here keeps a future regression from leaking a raw
    operator-controlled value into the query string.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# PromQL regex (RE2) special chars that must be escaped when interpolating a
# literal into `label=~"^prefix-.*"`. Applied BEFORE the label-value escape
# so the final wire string round-trips correctly through PromQL's
# string-unescape → regex compile.
_RE_SPECIAL = r".+*?()[]{}^$|\\"


def _escape_label_regex(s: str) -> str:
    """Escape a string for use as a regex literal inside a PromQL
    `label=~"<here>"` matcher.

    Two layers of escape: first turn regex metacharacters into their
    escaped form (`.` → `\\.`), then escape the result for the
    label-value string layer (`\\` → `\\\\`). PromQL parses the
    label value as a string FIRST (string-unescape), then compiles the
    result as a regex. So `app\\.api` on the wire:
      → string-unescape → `app\\.api` (one real backslash)
      → regex compile   → literal `.` (escaped)
      → matches "app.api" exactly, NOT "appXapi".

    Without this, the chart-1.13.0-shipped `pod=~"{target_name}-.*"` pattern
    cross-matched pods of similarly-named workloads (`app.api` matched
    `appXapi-pod-abc` because RE2 treats `.` as "any char").
    """
    # Escape regex specials first using re.escape, then label-string-escape.
    return _escape_label_value(re.escape(s))

_cache: dict[tuple, int | None] = {}
_mem_cache: dict[tuple, int | None] = {}
_req_cpu_cache: dict[tuple, int | None] = {}
_req_mem_cache: dict[tuple, int | None] = {}
# Cache for detect_scrape_interval — keyed by prometheus_url.
_scrape_interval_cache: dict[str, str] = {}

# Valid Prometheus duration literal (unit required; no bare integers).
# Accepts simple units (ms, s, m, h, d) and compound forms like "1m30s" or
# "1h30m" — kube-prometheus-stack can emit compound scrapeInterval values.
_DURATION_RE = re.compile(r"^(\d+(?:ms|[dhms]))+$")
# Regex for individual components parsed by _parse_duration_seconds.
_DURATION_COMPONENT_RE = re.compile(r"(\d+)(ms|[dhms])")
# Acceptable scrape interval range. Values outside this band are almost
# certainly a parse error or a misconfigured Prometheus — fall back to 15s.
_SCRAPE_INTERVAL_MIN_S = 5
_SCRAPE_INTERVAL_MAX_S = 120


def _parse_duration_seconds(s: str) -> int:
    """Convert a Prometheus duration string to whole seconds.

    Supports simple units (ms, s, m, h, d) and compound forms
    ("1m30s", "1h30m", etc.) — the full set accepted by _DURATION_RE.

    Sub-second values (e.g. "500ms") floor to 0.  Callers that use the
    result as a subquery step must check against _SCRAPE_INTERVAL_MIN_S
    and fall back when the value is below the minimum — 0s would produce
    an invalid PromQL subquery step.

    Raises ValueError for any string not matched by _DURATION_RE.
    """
    if not s:
        raise ValueError("empty duration string")
    _UNIT_SECS = {"ms": 0, "s": 1, "m": 60, "h": 3600, "d": 86400}
    total: float = 0.0
    matched_end = 0
    for m in _DURATION_COMPONENT_RE.finditer(s):
        if m.start() != matched_end:
            raise ValueError(f"invalid duration string: {s!r}")
        val, unit = int(m.group(1)), m.group(2)
        if unit not in _UNIT_SECS:
            raise ValueError(f"unknown duration unit: {unit!r} in {s!r}")
        if unit == "ms":
            total += val / 1000.0  # ms contributes fractional seconds
        else:
            total += val * _UNIT_SECS[unit]
        matched_end = m.end()
    if matched_end != len(s):
        raise ValueError(f"invalid duration string: {s!r}")
    return int(total)  # floor; sub-second (e.g. "500ms") floors to 0


def _irate_window_for_step(step: str) -> str:
    """Compute the irate lookback window that guarantees >= 2 samples per step.

    irate requires at least 2 samples in its lookback window to return a
    meaningful (non-NaN) value.  With a hardcoded 5m window that's fine for
    steps up to 150s (2 × 150 = 300 = 5m).  For larger steps the window must
    grow: lookback = max(300s, 2 × step_seconds).

    Returns a valid PromQL duration string ("5m", "360s", etc.).
    Falls back to "5m" for any unparseable or zero-length step.
    """
    try:
        step_secs = _parse_duration_seconds(step)
    except (ValueError, Exception):
        return "5m"
    if step_secs <= 0:
        return "5m"
    lookback_secs = max(300, 2 * step_secs)
    if lookback_secs == 300:
        return "5m"
    return f"{lookback_secs}s"


def detect_scrape_interval(prometheus_url: str) -> str:
    """Detect the scrape interval used for cAdvisor targets.

    Strategy (B-with-A-fallback):
      1. /api/v1/targets?state=active — filter targets whose
         `labels.metrics_path` ends with `/cadvisor` (these produce
         `container_cpu_usage_seconds_total`); use the smallest
         `scrapeInterval` found.
      2. Fallback: `global.scrape_interval` from /api/v1/status/config YAML
         (less accurate — the cadvisor job may override the global).
      3. Final fallback: "15s" (kube-prometheus-stack default).

    Result is clamped to _SCRAPE_INTERVAL_MIN_S–_SCRAPE_INTERVAL_MAX_S and
    cached per prometheus_url for the process lifetime.

    Why /api/v1/targets and not prometheus_target_interval_length_seconds:
    that summary carries the configured `interval` label but not the
    `metrics_path`, so we can't tell which interval belongs to cadvisor.
    The targets API gives per-target scrapeInterval + metrics_path together.
    """
    if not prometheus_url:
        return "15s"
    if prometheus_url in _scrape_interval_cache:
        return _scrape_interval_cache[prometheus_url]

    result = _detect_scrape_interval_uncached(prometheus_url)
    _scrape_interval_cache[prometheus_url] = result
    return result


def _detect_scrape_interval_uncached(prometheus_url: str) -> str:
    _FALLBACK = "15s"

    # --- Primary: /api/v1/targets, filter metrics_path ending /cadvisor ---
    try:
        resp = requests.get(
            f"{prometheus_url}/api/v1/targets",
            params={"state": "active"},
            timeout=15,
        )
        if resp.ok:
            active = resp.json().get("data", {}).get("activeTargets", [])
            cadvisor_intervals: list[str] = []
            for t in active:
                mp = t.get("labels", {}).get("metrics_path", "")
                si = t.get("scrapeInterval", "")
                if mp.endswith("/metrics/cadvisor") and _DURATION_RE.fullmatch(si):
                    cadvisor_intervals.append(si)
            if cadvisor_intervals:
                unique = set(cadvisor_intervals)
                if len(unique) > 1:
                    _log.warning(
                        "[prometheus] cAdvisor targets have mixed scrape intervals %s; "
                        "using smallest for subquery step alignment",
                        sorted(unique),
                    )
                chosen = min(cadvisor_intervals, key=_parse_duration_seconds)
                secs = _parse_duration_seconds(chosen)
                if _SCRAPE_INTERVAL_MIN_S <= secs <= _SCRAPE_INTERVAL_MAX_S:
                    _log.info(
                        "[prometheus] detected scrape interval %s for subquery step "
                        "(source: targets-api cadvisor)",
                        chosen,
                    )
                    return chosen
    except Exception as exc:
        _log.debug("[prometheus] targets API failed during scrape interval detection: %s", exc)

    # --- Fallback: global.scrape_interval from /api/v1/status/config YAML ---
    try:
        resp = requests.get(f"{prometheus_url}/api/v1/status/config", timeout=10)
        if resp.ok:
            yaml_str = resp.json().get("data", {}).get("yaml", "")
            m = re.search(
                r"^global:\s*\n(?:[ \t]+\S[^\n]*\n)*?[ \t]+scrape_interval:\s*(\S+)",
                yaml_str,
                re.MULTILINE,
            )
            if m:
                val = m.group(1)
                if _DURATION_RE.fullmatch(val):
                    secs = _parse_duration_seconds(val)
                    if _SCRAPE_INTERVAL_MIN_S <= secs <= _SCRAPE_INTERVAL_MAX_S:
                        _log.info(
                            "[prometheus] detected scrape interval %s for subquery step "
                            "(source: status-config global; cadvisor job may differ)",
                            val,
                        )
                        return val
    except Exception as exc:
        _log.debug("[prometheus] status/config failed during scrape interval detection: %s", exc)

    _log.warning(
        "[prometheus] scrape interval detection failed (no cadvisor targets via "
        "/api/v1/targets, no global.scrape_interval via /api/v1/status/config); "
        "using 15s fallback for subquery step — on non-standard backends "
        "(VictoriaMetrics, Thanos, non-cadvisor) the step may be misaligned with "
        "the actual scrape cadence, causing NaN gaps or spike undercounts in CPU "
        "limit estimates",
    )
    return _FALLBACK


def query_cpu_max_m(
    prometheus_url: str,
    namespace: str,
    container: str,
    target_name: str | None = None,
    window: str = "7d",
    subquery_step: str | None = None,
) -> int | None:
    """Return max instantaneous CPU usage in millicores over the given window.

    Uses irate[5m] to capture short startup spikes that rate[1m] smooths away.
    Results are cached in-process to avoid redundant queries.

    `subquery_step` overrides the subquery resolution (e.g. "10s"). When None,
    the step is auto-detected via detect_scrape_interval (once per process per
    URL, then cached). Aligning the step to the cAdvisor scrape interval avoids
    evaluating the subquery at points where no fresh sample exists.
    """
    if not prometheus_url:
        return None

    step = subquery_step if subquery_step is not None else detect_scrape_interval(prometheus_url)

    key = (prometheus_url, namespace, container, target_name, window, step)
    if key in _cache:
        return _cache[key]

    ns_e = _escape_label_value(namespace)
    c_e = _escape_label_value(container)
    pod_filter = f',pod=~"{_escape_label_regex(target_name)}-.*"' if target_name else ""
    # irate lookback must be >= 2 × step so a single missed scrape does not
    # produce NaN (which max_over_time silently skips, causing systematic
    # spike undercount). For normal steps (≤150s) this stays at the
    # traditional 5m; for larger steps it widens proportionally.
    irate_window = _irate_window_for_step(step)
    query = (
        f"max_over_time("
        f"irate(container_cpu_usage_seconds_total"
        f'{{namespace="{ns_e}",container="{c_e}"{pod_filter}}}[{irate_window}])'
        f"[{window}:{step}])"
    )
    try:
        resp = requests.get(f"{prometheus_url}/api/v1/query", params={"query": query}, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("data", {}).get("result", [])
        if not results:
            _log.warning("[prometheus] no CPU limit data for %s/%s (url: %s)", namespace, container, prometheus_url)
            _cache[key] = None
            return None
        max_cores = max(float(r["value"][1]) for r in results)
        result = round(max_cores * 1000)
        _cache[key] = result
        return result
    except Exception as exc:
        _log.warning("Prometheus CPU limit query failed (%s/%s): %s", namespace, container, exc)
        _cache[key] = None
        return None


def query_memory_max_bytes(
    prometheus_url: str,
    namespace: str,
    container: str,
    target_name: str | None = None,
    window: str = "7d",
) -> int | None:
    """Return max memory working set in bytes over the given window.

    Uses container_memory_working_set_bytes (what kubelet uses for OOM decisions).
    Results are cached in-process to avoid redundant queries.
    """
    if not prometheus_url:
        return None

    key = (prometheus_url, namespace, container, target_name, window)
    if key in _mem_cache:
        return _mem_cache[key]

    ns_e = _escape_label_value(namespace)
    c_e = _escape_label_value(container)
    pod_filter = f',pod=~"{_escape_label_regex(target_name)}-.*"' if target_name else ""
    query = (
        f"max_over_time("
        f"container_memory_working_set_bytes"
        f'{{namespace="{ns_e}",container="{c_e}"{pod_filter}}}[{window}])'
    )
    try:
        resp = requests.get(f"{prometheus_url}/api/v1/query", params={"query": query}, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("data", {}).get("result", [])
        if not results:
            _log.warning("[prometheus] no working set data for %s/%s (url: %s)", namespace, container, prometheus_url)
            _mem_cache[key] = None
            return None
        max_bytes = max(float(r["value"][1]) for r in results)
        result = round(max_bytes)
        _mem_cache[key] = result
        return result
    except Exception as exc:
        _log.warning("Prometheus memory limit query failed (%s/%s): %s", namespace, container, exc)
        _mem_cache[key] = None
        return None


def query_cpu_request_m(
    prometheus_url: str,
    namespace: str,
    container: str,
    target_name: str | None = None,
    percentile: float = 0.90,
    window: str = "3d",
) -> int | None:
    """Return CPU request estimate in millicores using quantile_over_time(rate[1m]).

    Mirrors VPA's percentile-based recommendation over a configurable window.
    """
    if not prometheus_url:
        return None

    key = (prometheus_url, namespace, container, target_name, percentile, window)
    if key in _req_cpu_cache:
        return _req_cpu_cache[key]

    ns_e = _escape_label_value(namespace)
    c_e = _escape_label_value(container)
    pod_filter = f',pod=~"{_escape_label_regex(target_name)}-.*"' if target_name else ""
    # quantile_over_time of the per-step max across pods:
    # 1. at each 1m step: max CPU across all pods → single time series per
    #    (namespace, container). `by(namespace, container)` is intentional:
    #    `without(pod)` would leave node/uid/etc. as surviving label dimensions,
    #    so Prometheus returns one series per surviving combination instead of one
    #    per workload — Python max() over that list silently returns the highest
    #    p90 in the namespace (cross-workload contamination). `by(namespace,
    #    container)` collapses all dimensions except namespace+container, so
    #    Prometheus always returns exactly 1 series and max() is a no-op.
    # 2. over window: take percentile of that series
    # This uses the full historical window and is immune to rollout outlier pods.
    query = (
        f"quantile_over_time({percentile},"
        f"max by(namespace, container)("
        f"rate(container_cpu_usage_seconds_total"
        f'{{namespace="{ns_e}",container="{c_e}"{pod_filter}}}[1m])'
        f")[{window}:1m])"
    )
    try:
        resp = requests.get(f"{prometheus_url}/api/v1/query", params={"query": query}, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("data", {}).get("result", [])
        if not results:
            _log.warning("[prometheus] no CPU request data for %s/%s (url: %s)", namespace, container, prometheus_url)
            _req_cpu_cache[key] = None
            return None
        max_cores = max(float(r["value"][1]) for r in results)
        result = round(max_cores * 1000)
        _req_cpu_cache[key] = result
        return result
    except Exception as exc:
        _log.warning("Prometheus CPU request query failed (%s/%s): %s", namespace, container, exc)
        _req_cpu_cache[key] = None
        return None


def query_mem_request_bytes(
    prometheus_url: str,
    namespace: str,
    container: str,
    target_name: str | None = None,
    percentile: float = 0.90,
    window: str = "8d",
) -> int | None:
    """Return memory request estimate in bytes using quantile_over_time over a sliding window."""
    if not prometheus_url:
        return None

    key = (prometheus_url, namespace, container, target_name, percentile, window)
    if key in _req_mem_cache:
        return _req_mem_cache[key]

    ns_e = _escape_label_value(namespace)
    c_e = _escape_label_value(container)
    pod_filter = f',pod=~"{_escape_label_regex(target_name)}-.*"' if target_name else ""
    # `by(namespace, container)` collapses pods AND all other label dimensions
    # (node, uid, etc.) so Prometheus returns exactly 1 series per workload.
    # `without(pod)` would leave extra dimensions → multi-series result →
    # Python max() returns the cross-namespace highest p90 (contamination bug).
    query = (
        f"quantile_over_time({percentile},"
        f"max by(namespace, container)("
        f"container_memory_working_set_bytes"
        f'{{namespace="{ns_e}",container="{c_e}"{pod_filter}}}'
        f")[{window}:5m])"
    )
    try:
        resp = requests.get(f"{prometheus_url}/api/v1/query", params={"query": query}, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("data", {}).get("result", [])
        if not results:
            _log.warning("[prometheus] no memory request data for %s/%s (url: %s)", namespace, container, prometheus_url)
            _req_mem_cache[key] = None
            return None
        max_bytes = max(float(r["value"][1]) for r in results)
        result = round(max_bytes)
        _req_mem_cache[key] = result
        return result
    except Exception as exc:
        _log.warning("Prometheus memory request query failed (%s/%s): %s", namespace, container, exc)
        _req_mem_cache[key] = None
        return None


def prefetch_prometheus_parallel(
    entries: list[tuple[str, str, str | None]],
    prometheus_url: str,
    cpu_percentile: float,
    cpu_window: str,
    mem_percentile: float,
    mem_window: str,
    cpu_limit_window: str = "7d",
    mem_limit_window: str = "7d",
    max_workers: int = 4,
) -> None:
    """Pre-populate in-process query caches for all containers in parallel.

    entries: list of (namespace, container_name, workload_name)
    Fires all 4 queries per container concurrently; subsequent calls in the same
    process hit the in-memory cache immediately.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    unique = list(dict.fromkeys(entries))
    if not unique:
        return

    if not prometheus_url:
        _log.warning(
            "[prometheus] no URL configured — all queries will return None; "
            "resource values will not be updated"
        )
        return

    # Detect the cAdvisor subquery step once before spawning workers so the
    # detection log line appears in the prefetch context (not inside a thread)
    # and all workers share the resolved step.
    step = detect_scrape_interval(prometheus_url)

    _log.debug("[prometheus] prefetching %d container(s) in parallel (step=%s)", len(unique), step)

    def _fetch(namespace: str, container: str, workload: str | None) -> None:
        query_cpu_max_m(prometheus_url, namespace, container, workload, cpu_limit_window, step)
        query_memory_max_bytes(prometheus_url, namespace, container, workload, mem_limit_window)
        query_cpu_request_m(prometheus_url, namespace, container, workload,
                            cpu_percentile, cpu_window)
        query_mem_request_bytes(prometheus_url, namespace, container, workload,
                                mem_percentile, mem_window)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for f in as_completed([executor.submit(_fetch, *e) for e in unique]):
            f.result()


def check_connectivity(url: str) -> str:
    """Probe the Prometheus URL and return a status string.

    Returns "OK (version: <ver>)" on success, "HTTP <code>" on bad response,
    "UNREACHABLE: <err>" on exception. Empty input returns "no URL configured".
    """
    if not url:
        return "no URL configured"
    try:
        resp = requests.get(f"{url}/api/v1/status/buildinfo", timeout=10)
        if resp.ok:
            version = resp.json().get("data", {}).get("version", "?")
            return f"OK (version: {version})"
        return f"HTTP {resp.status_code}"
    except Exception as exc:
        return f"UNREACHABLE: {exc}"
