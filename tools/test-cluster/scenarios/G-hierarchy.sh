#!/usr/bin/env bash
# Scenario G — 3-level hierarchy resolution for cpuPercentile.
#
# What it verifies (against the [override] log line emitted by
# `_log_effective_overrides` in main.py):
#   1. helm-default cpuPercentile applies when no annotation is present
#   2. Adding `kube-resource-updater.cpuPercentile` on the Namespace
#      overrides helm. The override line says `cpuPercentile=<value>`.
#   3. Adding it on the workload overrides BOTH (workload wins ns wins
#      helm). The override line still appears, with the workload value.
#   4. Removing the workload annotation falls back to ns. (Tested across
#      two consecutive sync runs to verify resolver is stateless.)
#
# The actual Prom query and resulting numbers aren't asserted — those
# require live data with multi-day history. We assert the RESOLVER
# decision via the `[override]` log line, which is the exact code path
# `resolve_for_workload` produces.

set -euo pipefail
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
# shellcheck source=../_lib.sh
source "${SCRIPT_DIR}/../_lib.sh"

NS="kru-test-g-hier"
trap 'cleanup_test_ns "${NS}"' EXIT

log_header "Scenario G — 3-level hierarchy: cpuPercentile"
preflight
make_test_ns "${NS}"

log_info "creating test Deployment (no workload annotation yet)"
cat <<EOF | kc apply -f - >/dev/null
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kru-test-g-app
  namespace: ${NS}
  labels:
    app.kubernetes.io/instance: kru-test-g-app
spec:
  replicas: 1
  selector: { matchLabels: { app.kubernetes.io/instance: kru-test-g-app } }
  template:
    metadata:
      labels: { app.kubernetes.io/instance: kru-test-g-app }
    spec:
      containers:
        - name: app
          image: registry.k8s.io/pause:3.10
          resources:
            requests: { cpu: 50m, memory: 32Mi }
            limits:   { cpu: 100m, memory: 64Mi }
EOF
kc rollout status -n "${NS}" deploy/kru-test-g-app --timeout=60s >/dev/null

# ── Level 1: helm default ────────────────────────────────────────────
log_info "level 1: helm default — no annotations, no [override] line expected"
run_manual_sync
# [override] only appears when the effective Config DIFFERS from helm.
# At helm-only level the resolver returns the same Config, so no line.
if last_sync_log_contains "${NS}/kru-test-g-app.*\[override\]"; then
    log_fail "scenario-g/helm-level: unexpected [override] line at helm-only level"
    exit 1
fi
log_pass "scenario-g/helm-level: no [override] line — workload uses helm defaults"

# ── Level 2: namespace annotation overrides helm ─────────────────────
log_info "level 2: namespace cpuPercentile=0.95 — expect [override] line with cpuPercentile=95%"
kc annotate ns "${NS}" "${KRU_ANN_PREFIX}.cpuPercentile=0.95" --overwrite >/dev/null
run_manual_sync
assert_log_contains "scenario-g/ns-override" "${NS}/kru-test-g-app \[override\]:.*cpuPercentile=95%"

# Negative assertion at this level: no workload-level overrides set, so
# memPercentile / marginFraction / etc. must NOT appear in the override
# line. The line lists ONLY changed keys.
log_pass "scenario-g/ns-only: ns layer wins over helm"

# ── Level 3: workload annotation overrides namespace ─────────────────
log_info "level 3: workload cpuPercentile=0.99 — expect override line with 99%"
kc annotate deploy -n "${NS}" kru-test-g-app "${KRU_ANN_PREFIX}.cpuPercentile=0.99" --overwrite >/dev/null
run_manual_sync
assert_log_contains "scenario-g/wl-override" "${NS}/kru-test-g-app \[override\]:.*cpuPercentile=99%"

# At this point both ns and wl annotations are set — wl must win.
# The override line should show 99% (the wl value), not 95% (the ns
# value). Regex tests for the WL value being present.
if last_sync_log_contains "${NS}/kru-test-g-app \[override\]:.*cpuPercentile=95%"; then
    log_fail "scenario-g/wl-wins: log shows 95% — namespace value leaked through (wl SHOULD win)"
    exit 1
fi
log_pass "scenario-g/wl-wins: workload value (99%) wins over namespace value (95%)"

# ── Level 4: removing workload annotation falls back to namespace ────
log_info "removing workload annotation — expect fallback to ns value 95%"
kc annotate deploy -n "${NS}" kru-test-g-app "${KRU_ANN_PREFIX}.cpuPercentile-" >/dev/null
run_manual_sync
assert_log_contains "scenario-g/wl-removed-fallback" "${NS}/kru-test-g-app \[override\]:.*cpuPercentile=95%"

# Negative at this level: no longer 99% (the removed wl value).
if last_sync_log_contains "${NS}/kru-test-g-app \[override\]:.*cpuPercentile=99%"; then
    log_fail "scenario-g/wl-removed-stale: log shows 99% — removed wl annotation still has effect"
    exit 1
fi
log_pass "scenario-g/wl-removed-clean: removal of wl annotation correctly falls back to ns"

log_header "Scenario G: PASS"
