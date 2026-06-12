#!/usr/bin/env bash
# Shared helpers for integration scenarios. Sourced by every script under
# `tools/test-cluster/scenarios/`.
#
# Default target is `el-ops-cluster` (real cluster — fast, reproducible,
# already has Prometheus + ArgoCD + GitLab wired). For disruptive phases
# (failure injection, scale, upgrade) point at a `kind` cluster by
# setting `KRU_CONTEXT=kind-kru-test` (the script makes no assumption
# about which cluster — only that `kubectl --context $KRU_CONTEXT` works
# and the chart is installed in $KRU_NS).
#
# Every scenario script is expected to:
#   1. Pick a unique test namespace `kru-test-<scenario-id>`.
#   2. Create the namespace WITHOUT the opt-in annotation first.
#   3. Apply its test workload(s).
#   4. Add the opt-in annotation.
#   5. Trigger a manual sync (or wait for the CronJob).
#   6. Assert against `kubectl get ro -n <ns>` + the sync logs.
#   7. Tear down the test namespace at exit (trap).
#
# Exit codes:
#   0 — all asserts passed
#   1 — assert failed; namespace left for inspection unless KRU_CLEAN=1
#   2 — setup error (chart not found, kubectl can't reach context)

set -euo pipefail

KRU_CONTEXT="${KRU_CONTEXT:-el-ops-cluster}"
KRU_NS="${KRU_NS:-kube-resource-updater}"
KRU_ANN_PREFIX="${KRU_ANN_PREFIX:-kube-resource-updater}"
KRU_CLEAN="${KRU_CLEAN:-1}"
KRU_VERBOSE="${KRU_VERBOSE:-0}"

# Color output — gray (cinza) = info, green = pass, red = fail. Mirrors
# the tool's own palette so the integration-test output is visually
# consistent with the deployed pod's logs.
if [[ -t 1 ]]; then
    C_INFO='\033[38;5;240m'
    C_PASS='\033[38;5;34m'
    C_FAIL='\033[38;5;124m'
    C_HEADER='\033[1;38;5;25m'
    C_RESET='\033[0m'
else
    C_INFO=''; C_PASS=''; C_FAIL=''; C_HEADER=''; C_RESET=''
fi

log_info()   { echo -e "${C_INFO}[info]${C_RESET} $*" >&2; }
log_pass()   { echo -e "${C_PASS}[ok]${C_RESET}   $*" >&2; }
log_fail()   { echo -e "${C_FAIL}[FAIL]${C_RESET} $*" >&2; }
log_header() { echo -e "\n${C_HEADER}── $* ───────────${C_RESET}\n" >&2; }

# kc — kubectl with the test context baked in. Every kubectl call should
# go through this so the same script works against el-ops or kind.
kc() {
    kubectl --context "${KRU_CONTEXT}" "$@"
}

# Bail out early if the cluster context isn't reachable or the chart
# isn't installed. Catches "wrong cluster", "no kubeconfig", "chart
# never installed" before the scenario starts allocating resources.
preflight() {
    if ! kc get ns "${KRU_NS}" >/dev/null 2>&1; then
        log_fail "namespace '${KRU_NS}' not found on context '${KRU_CONTEXT}' — is the chart installed?"
        exit 2
    fi
    if ! kc get cronjob -n "${KRU_NS}" kube-resource-updater >/dev/null 2>&1; then
        log_fail "CronJob 'kube-resource-updater' not found in '${KRU_NS}' on '${KRU_CONTEXT}'"
        exit 2
    fi
    log_info "context: ${KRU_CONTEXT}, chart namespace: ${KRU_NS}"
}

# Create + opt-in the test namespace. Idempotent — if the namespace
# already exists (previous scenario didn't clean up), wipe it first.
make_test_ns() {
    local ns="$1"
    if kc get ns "${ns}" >/dev/null 2>&1; then
        log_info "test namespace ${ns} already exists, wiping"
        kc delete ns "${ns}" --wait=true --timeout=60s
    fi
    kc create ns "${ns}"
    kc annotate ns "${ns}" "${KRU_ANN_PREFIX}.enabled=true"
    log_info "created test namespace ${ns} with opt-in annotation"
}

# Tear down the test namespace at scenario exit. Trapped on EXIT so it
# runs whether the scenario passed or failed. `KRU_CLEAN=0` skips
# teardown — useful when debugging a fail.
cleanup_test_ns() {
    local ns="$1"
    if [[ "${KRU_CLEAN}" != "1" ]]; then
        log_info "KRU_CLEAN=0 — leaving namespace ${ns} for inspection"
        return
    fi
    kc delete ns "${ns}" --wait=false 2>/dev/null || true
    log_info "tore down ${ns}"
}

# Trigger a one-shot manual run of the CronJob and block until it
# finishes (Succeeded or Failed). Returns the Job's exit message.
run_manual_sync() {
    local job_name="kru-test-$(date +%s)-$RANDOM"
    kc delete job -n "${KRU_NS}" "${job_name}" --ignore-not-found >/dev/null 2>&1
    kc create job -n "${KRU_NS}" --from=cronjob/kube-resource-updater "${job_name}" >/dev/null
    if [[ "${KRU_VERBOSE}" == "1" ]]; then
        log_info "triggered manual sync as job/${job_name}"
    fi
    # Wait up to 5 min for the Job to finish. The first manual run on a
    # cold pod can take ~30-60s once the image is pulled.
    kc wait --for=condition=complete --timeout=300s "job/${job_name}" -n "${KRU_NS}" >/dev/null 2>&1 \
        || kc wait --for=condition=failed --timeout=10s "job/${job_name}" -n "${KRU_NS}" >/dev/null 2>&1 \
        || true
    LAST_JOB="${job_name}"
}

# Strip ANSI color codes from a stream. The tool's text-mode log emits
# 256-color SGR escapes (\x1b[38;5;NNNm ... \x1b[0m); leaving them in
# breaks `grep` regexes that use [^a-zA-Z]* between values because the
# `m` terminator of an ANSI escape IS a letter. Filter once at the
# log-fetch boundary so every assert sees plain text.
_strip_ansi() {
    sed -E 's/\x1b\[[0-9;]*m//g'
}

# Grep the most-recent sync's pod logs for a pattern. Used by scenario
# asserts that check "did the tool log [X] for namespace Y?".
last_sync_log_contains() {
    local pattern="$1"
    kc logs -n "${KRU_NS}" -l "job-name=${LAST_JOB}" --tail=500 2>/dev/null | _strip_ansi | grep -q -E "${pattern}"
}

last_sync_log_count() {
    local pattern="$1"
    kc logs -n "${KRU_NS}" -l "job-name=${LAST_JOB}" --tail=500 2>/dev/null | _strip_ansi | grep -c -E "${pattern}" || true
}

last_sync_log() {
    kc logs -n "${KRU_NS}" -l "job-name=${LAST_JOB}" --tail=500 2>/dev/null | _strip_ansi
}

# Block until a ResourceOverride appears in the test namespace, or
# timeout. Polls every 2s. Asserts the CR exists + returns its name.
wait_for_cr() {
    local ns="$1"
    local timeout="${2:-60}"
    local elapsed=0
    while (( elapsed < timeout )); do
        local count
        count=$(kc get ro -n "${ns}" --no-headers 2>/dev/null | wc -l)
        if (( count > 0 )); then
            kc get ro -n "${ns}" --no-headers | awk '{print $1}' | head -1
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    return 1
}

# Assert helpers. Standardised PASS/FAIL output + exit-on-fail with a
# named assertion so the runner's summary table is filled in correctly.
assert_eq() {
    local label="$1" got="$2" want="$3"
    if [[ "${got}" == "${want}" ]]; then
        log_pass "${label}: got ${got}"
    else
        log_fail "${label}: expected ${want}, got ${got}"
        exit 1
    fi
}

assert_contains() {
    local label="$1" haystack="$2" needle="$3"
    if [[ "${haystack}" == *"${needle}"* ]]; then
        log_pass "${label}: matched"
    else
        log_fail "${label}: '${needle}' not found in output"
        log_fail "  got: ${haystack}"
        exit 1
    fi
}

assert_log_contains() {
    local label="$1" pattern="$2"
    if last_sync_log_contains "${pattern}"; then
        log_pass "${label}: log matched '${pattern}'"
    else
        log_fail "${label}: log did NOT contain '${pattern}'"
        log_fail "  ---- last sync log: ----"
        last_sync_log | sed 's/^/    /' | tail -30 >&2
        exit 1
    fi
}

assert_log_not_contains() {
    local label="$1" pattern="$2"
    if last_sync_log_contains "${pattern}"; then
        log_fail "${label}: log SHOULD NOT contain '${pattern}'"
        exit 1
    fi
    log_pass "${label}: log clean of '${pattern}'"
}
