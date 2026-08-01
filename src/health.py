"""Sample-quality gates for the recommendation phase.

The recommender is a percentile over a Prometheus window. That is only a
recommendation if the samples in the window describe the workload doing its
job. Two situations produce samples that describe something else entirely:

  1. **The source pods were crash-looping.** What lands in the window is a
     series of failed startups — a burst of CPU while the process boots,
     then nothing, repeated. The percentile of that is not the workload's
     steady-state usage, and a request sized from it is far too small for
     the workload once it starts working. Because the admission webhook
     re-applies the stored CR values on every restart and never recomputes,
     an undersized CR keeps the workload restarting, which keeps the samples
     bad. The loop does not break on its own.

  2. **There were barely any samples.** A workload deployed an hour before
     the sync, a Prometheus outage, retention shorter than the configured
     window — the percentile is computed, it just isn't backed by anything.
     Same class of garbage, different cause.

Both gates answer one question: *should this workload's recommendation be
recomputed from the current window at all?* They deliberately do not answer
"what should the value be" — when a gate trips the caller preserves what is
already committed to the CR. Nothing here decides resource values.

Signal source is Prometheus for both, over the same window that feeds the
percentile. See `query_restart_count` in `src/prometheus.py` for why the
live pod API's `restartCount` is the wrong instrument for gate 1.

Everything in this module is Prometheus-in, verdict-out. `src/prometheus.py`
stays a dumb query layer with no notion of health; the policy lives here.
"""
from __future__ import annotations

from dataclasses import dataclass

from src import log as _log_module
from src.prometheus import (
    _parse_duration_seconds,
    query_cpu_sample_count,
    query_mem_sample_count,
    query_restart_count,
)

_log = _log_module.get(__name__)

# Verdict reason codes. Stable strings — they surface in log lines and in the
# MR description, so operators grep for them.
HOLD_RESTARTS = "restarts"
HOLD_INSUFFICIENT_DATA = "insufficient-data"

# Subquery steps of the two request queries in `src/prometheus.py`. The
# coverage queries mirror them, so the expected-sample counts must use the
# same values.
_CPU_REQUEST_STEP = "1m"
_MEM_REQUEST_STEP = "5m"

# One warning per process when the restart metric is missing everywhere. The
# sync runs as a CronJob (one process per run), so this is one line per sync
# rather than one per container.
_restart_metric_warned = False


@dataclass(frozen=True)
class HoldVerdict:
    """Whether a container's recommendation may be recomputed this sync.

    `detail` is the operator-facing sentence — it goes into the log line and
    into the MR description verbatim, so it has to name the numbers that
    caused the decision.
    """
    held: bool
    reason: str = ""
    detail: str = ""


NOT_HELD = HoldVerdict(False)


def expected_sample_count(window: str, step: str) -> int:
    """How many evaluation points a fully-covered `[window:step]` subquery has.

    Returns 0 when either duration is unparseable or floors to zero seconds
    (e.g. a `500ms` step). Callers treat 0 as "unknown" and skip the
    coverage gate rather than dividing by it.
    """
    try:
        window_s = _parse_duration_seconds(window)
        step_s = _parse_duration_seconds(step)
    except ValueError:
        return 0
    if window_s <= 0 or step_s <= 0:
        return 0
    return window_s // step_s


def widest_window(*windows: str) -> str:
    """Return the longest of the given Prometheus durations.

    The restart gate runs once per container over the widest request window,
    not once per query: a crash loop anywhere in the data that feeds any
    request percentile is enough to distrust the whole recommendation.
    Unparseable values sort as zero-length and only win if nothing else
    parses.
    """
    best, best_secs = "", -1
    for w in windows:
        if not w:
            continue
        try:
            secs = _parse_duration_seconds(w)
        except ValueError:
            secs = 0
        if secs > best_secs:
            best, best_secs = w, secs
    return best


def evaluate_restart_gate(
    restarts: int | None,
    max_restarts: int,
    window: str,
) -> HoldVerdict:
    """Hold when the workload's pods restarted too often inside the window.

    `restarts is None` means the counter is absent (no kube-state-metrics).
    That fails OPEN: holding every workload in the cluster because a metric
    is missing would be a far worse regression than the bug this gate
    closes, and the operator gets a warning telling them the gate is inert.

    `max_restarts <= 0` disables the gate.
    """
    global _restart_metric_warned

    if max_restarts <= 0:
        return NOT_HELD
    if restarts is None:
        if not _restart_metric_warned:
            _restart_metric_warned = True
            _log.warning(
                "[health] kube_pod_container_status_restarts_total returned no "
                "data — the crash-loop gate is inert this sync and "
                "recommendations may be computed from unhealthy pods. Install "
                "kube-state-metrics, or set config.healthGateEnabled=false to "
                "silence this."
            )
        return NOT_HELD
    if restarts <= max_restarts:
        return NOT_HELD
    return HoldVerdict(
        True, HOLD_RESTARTS,
        f"{restarts} container restart(s) in {window} (max {max_restarts})",
    )


def evaluate_coverage_gate(
    samples: int | None,
    expected: int,
    min_coverage: float,
    window: str,
    label: str,
) -> HoldVerdict:
    """Hold when too few of the window's evaluation points carried data.

    `samples is None` (query failed, or no series at all) fails OPEN: a
    container with no series produces no request/limit values either, and
    the existing "no Prometheus data" path in `_build_container_resources`
    already skips it. Adding a second verdict for the same situation would
    only make the logs contradict each other.

    `expected <= 0` (unparseable window/step) and `min_coverage <= 0` both
    disable the gate.
    """
    if min_coverage <= 0 or expected <= 0 or samples is None:
        return NOT_HELD
    coverage = samples / expected
    if coverage >= min_coverage:
        return NOT_HELD
    return HoldVerdict(
        True, HOLD_INSUFFICIENT_DATA,
        f"{label} percentile backed by {samples}/{expected} samples over "
        f"{window} ({coverage:.0%} < {min_coverage:.0%} required)",
    )


def assess_container(
    prometheus_url: str,
    namespace: str,
    container: str,
    target_name: str | None,
    rc,
) -> HoldVerdict:
    """Run both gates for one container and return the first hold.

    `rc` is the workload's **effective** ResourceConfig (already merged
    through helm < namespace < workload), so the thresholds and windows are
    the per-workload ones.

    Short-circuits: the restart query runs first because a crash loop is the
    expensive failure mode, and a held container skips the value queries
    entirely.
    """
    if not prometheus_url:
        return NOT_HELD

    restart_window = widest_window(rc.cpu_request_window, rc.mem_request_window)
    verdict = evaluate_restart_gate(
        query_restart_count(prometheus_url, namespace, container, target_name, restart_window),
        rc.max_restarts_in_window,
        restart_window,
    )
    if verdict.held:
        return verdict

    verdict = evaluate_coverage_gate(
        query_cpu_sample_count(prometheus_url, namespace, container, target_name,
                               rc.cpu_request_window),
        expected_sample_count(rc.cpu_request_window, _CPU_REQUEST_STEP),
        rc.min_sample_coverage,
        rc.cpu_request_window,
        "CPU",
    )
    if verdict.held:
        return verdict

    return evaluate_coverage_gate(
        query_mem_sample_count(prometheus_url, namespace, container, target_name,
                               rc.mem_request_window),
        expected_sample_count(rc.mem_request_window, _MEM_REQUEST_STEP),
        rc.min_sample_coverage,
        rc.mem_request_window,
        "memory",
    )
