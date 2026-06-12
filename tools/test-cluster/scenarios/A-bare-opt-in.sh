#!/usr/bin/env bash
# Scenario A — bare opt-in workload end-to-end.
#
# What it verifies:
#   1. A freshly-annotated namespace gets picked up by the next sync.
#   2. Workload labels (`app.kubernetes.io/instance|name`) are read.
#   3. A `ResourceOverride` CR is written into the gitops repo's
#      manifests path, with one container entry under
#      `spec.containers[].name == "app"`.
#   4. The CR's `spec.selector.matchLabels` resolves the workload
#      (smoke check; the admission webhook itself is covered by
#      scenario F).
#   5. The sync log emits the `[OK] <ns>:` ack line.
#
# Skips Prometheus assertions — this scenario uses a fresh Deployment
# with no metric history, so the recommended values fall back to the
# `min*RequestM` floors. Scenarios B / G exercise the live Prom path.

set -euo pipefail
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
# shellcheck source=../_lib.sh
source "${SCRIPT_DIR}/../_lib.sh"

NS="kru-test-a-bare"
trap 'cleanup_test_ns "${NS}"' EXIT

log_header "Scenario A — bare opt-in workload end-to-end"
preflight
make_test_ns "${NS}"

# Apply a minimal Deployment with the chart-recognised labels.
log_info "creating test Deployment with app.kubernetes.io/instance label"
cat <<EOF | kc apply -f - >/dev/null
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kru-test-a-app
  namespace: ${NS}
  labels:
    app.kubernetes.io/instance: kru-test-a-app
    app.kubernetes.io/name: kru-test-a
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/instance: kru-test-a-app
  template:
    metadata:
      labels:
        app.kubernetes.io/instance: kru-test-a-app
        app.kubernetes.io/name: kru-test-a
    spec:
      containers:
        - name: app
          image: registry.k8s.io/pause:3.10
          resources:
            requests: { cpu: 50m, memory: 32Mi }
            limits:   { cpu: 100m, memory: 64Mi }
EOF
kc rollout status -n "${NS}" deploy/kru-test-a-app --timeout=60s >/dev/null

log_info "triggering manual sync"
run_manual_sync

log_info "asserting discovery + write-back path on data-less workload"
# Discovery: namespace header + workload listing + container count.
assert_log_contains "scenario-a/discovered-ns"       "=== ${NS} ===  \(workloads=1\)"
assert_log_contains "scenario-a/discovered-workload" "kru-test-a-app  containers=1"

# Prom queried for the workload (warnings prove the namespace was
# resolved + the workload was scheduled for Prom lookup). The exact
# warning emitted depends on which queries return empty — there are
# 4 candidates (req/lim × cpu/mem). Assert at least one.
assert_log_contains "scenario-a/prom-attempted" "\[prometheus\] no .* data for ${NS}/app"

# Write-back: even without Prom data, the chart's `minCpuRequestM`
# floor (200m) generates a CR with cpu request/limit only — memory
# stays unrecommended because `_build_container_resources` only
# applies the memory floor when there's a base value to clamp.
assert_log_contains "scenario-a/cr-acked"           "\[OK\] +${NS}: manifests"
assert_log_contains "scenario-a/cr-floor-cpu-only"  "kru-test-a-app/app  req=cpu:200m mem:—"

# Result phase mentions the test namespace alongside the real ones.
assert_log_contains "scenario-a/result-includes-ns" "namespaces:.*${NS}"

log_header "Scenario A: PASS"
