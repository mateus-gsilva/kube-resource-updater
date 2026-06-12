# Pre-release Integration Test Plan

> **Audience:** contributors and reviewers running the full integration
> matrix before a chart release goes out (typical cadence: every
> `MAJOR.MINOR` bump; not every patch).
>
> **Scope:** what the unit-level QA in `tools/qa_params.py` can NOT see —
> sequencing across apiserver + webhook + kubelet, real Prometheus
> behaviour under load, GitLab API behaviour under failure, upgrade
> from older chart versions, and OSS sanity (helm/yaml/image/deps
> scans).

This is a **reproducible-from-scratch** plan: every command runs as
written on a clean machine with `docker` + `kind` + `kubectl` + `helm`
+ `python3` + standard scanners installed. Output of each phase goes
into `tools/test-cluster/output/<phase>.log` for diff-against-baseline.

## Three-layer testing strategy

| Layer | Where | How | Speed | What it catches |
|---|---|---|---|---|
| **L1: Unit QA** | `tools/qa_params.py` (560+ asserts, 23 sections) | Mocks the apiserver and Prometheus. Tests every pair of toggles that interact in code. | <30s, every PR | Pure-function regressions, helm-render conditional gates, Config validation, resolver hierarchy |
| **L2: Integration scenarios** | `tools/test-cluster/scenarios/A-J.sh` (kind cluster) | End-to-end: one scenario = one specific behaviour with one workload. Asserts via `kubectl get` + log greps. | ~10min full sweep | Sequencing across apiserver + webhook + kubelet, real watch + admission flow |
| **L3: Combinatorial matrix** | `tools/test-cluster/combinatorial/` (NEW) | **Pairwise (AllPairs)** over a curated set of toggles. Auto-generates ~25-30 input tuples covering every pair of values. | ~30min | Multi-toggle interactions that L1's "pair-aware" coverage misses at integration level |

### Why pairwise (and not brute-force)

17 boolean toggles in this chart = 2^17 = 131,072 combinations. NIST's
empirical study (Kuhn, Wallace, Gallo 2004) on bug data from web
browsers, kernels, and medical apps found that:

- ~70% of bugs are triggered by **1 variable** (single-input tests catch them)
- ~25% by **interaction of 2 variables** (pairs)
- ~4% by **3 variables**
- ~1% by **4+ variables**

Covering every PAIR catches ~95% of bugs. Pairwise algorithms (AllPairs,
PICT, ACTS) find ~25-30 test cases that cover all 544 distinct pair-values
of 17 binary toggles. **Curated** inputs further drop the count by removing
pairs that don't interact in code (e.g. `networkPolicy.enabled` ×
`oomFloorEnabled` are independent — separate code paths, no test needed).

Per-PR contract (lightweight version): every new toggle/feature ships
with **1 targeted assert per pair of interacting existing toggles**. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) — "Tests are the contract".

## Code-derived coverage map (what the source actually does)

Not naïve enumeration of `values.yaml` — actual decision points found
by reading the code. Each row names a module + the test layer where
it's covered.

| Module | Decision points | Coverage |
|---|---|---|
| `src/writeback_webhook._build_containers_payload` | freeze warning (grow ∧ shrink), bounds, skip-containers filter, oom_floor_reset, OOM bump path, post-floor enforce, bump-suppression detect, per-container annotation map | L1 `section_oom_slow_path`, `section_grow_shrink`, `section_floors`, `section_skip_containers` (~120 asserts). **L2 gap**: live freeze+OOM-suppress scenario. |
| `src/writeback_webhook._commit_repo` | createMr bucket split (pass-1 direct + pass-2 MR), orphan-file cleanup deferral, MR metadata, 2-pass git checkout, push.useForceWithLease handling | L1 `section_create_mr_bucketing` (~12 asserts). **L2 gap**: live 2-pass bucket with real GitLab. |
| `src/webhook_patch.build_patches` | per-container override merge (last-write-wins on name collision), `/metadata/annotations` add when pod has none vs key add when it does, containers+initContainers both walked | L1: covered indirectly by webhook server tests. **L2 gap**: live pod with NO annotations + override (the JSONPatch `add /metadata/annotations` corner case). |
| `src/webhook_cache.ResourceOverrideCache` | bootstrap silent, reconnect with synthetic events, watch reconnect on 410/504/EOF, resource_version advance, callback exception isolation | L1 `section_cr_cache_reconnect` (~10 asserts). **L2 gap**: real watch interruption (kubectl delete pod mid-flight). |
| `src/webhook_cache.NamespaceCache` | enabled-annotation parsing, opt-in filter, dict copy on lookup | L1 `section_namespace_cache` (~8 asserts). |
| `src/webhook_rollout.RolloutDaemon` | ADDED schedules, MODIFIED with resource-change schedules, MODIFIED without resource-change skips, DELETED skips, debounce coalesce, 3-level hierarchy, Deployment vs StatefulSet patch dispatch | L1 `section_auto_rollout` (~30 asserts). **L2 gap**: end-to-end 3-level hierarchy on real CRs. Already live-validated on the author's production cluster (chart 1.5.0+). |
| `src/webhook_cert.CertReconciler` | new-cert generation, existing-secret with valid cert, existing-secret with expired cert, apiserver patch idempotence (`Already up to date` no-op) | L1 `section_cert_reconciler` (~16 asserts). **L2 gap**: cert rotation under admission burst (long-lived cluster, observe at expiry boundary). |
| `src/webhook_status.StatusUpdater` | dirty-set coalesce (multiple `record()` → 1 PATCH per CR per flush), 404 swallow on deleted-CR race, body shape `{lastAppliedAt}` only (no `appliedToPodCount`) | L1 `section_status_updater` (~12 asserts). **L2 gap**: replicaCount>1 race (two webhook replicas both stamping the same CR). |
| `src/webhook_validate` | selector overlap detection, container-name conflict detection, allow on first-seen CR | L1 `section_validating_webhook` (~20 asserts). |
| `src/overrides.resolve_for_workload` | helm < ns < workload hierarchy resolution, unknown-key typo warning, bool coercion, numeric range, every camelCase key → resolver chain | L1 `section_resolver` (~140 asserts — table-driven matrix). |
| `src/config.Config.validate` | required keys (repoUrl, path, prometheusUrl), min>max bounds, percentile/multiplier/bumpFactor range, malformed Prom duration, createMr+token consistency | L1 `section_config_validate` (~40 asserts). **L2 ✅ live-validated** chart 1.20.0 (runtime exit 2 on hand-edited ConfigMap). |
| `src/log` | tag padding, phase banner (bold+UPPERCASE+dashes), inline coloring (URL+arrow+keyword+pct), unchanged-line dim, JSON tag/phase fields, `_resolve_color` tri-state | L1 `section_log_formatter` (~70 asserts). |
| `templates/validate.yaml` | both-off fail, replicaCount=0 with autoRollout fail, createMr+no-token fail, prometheusUrl empty fail | L1 `section_chart_conditional_rbac` (~25 asserts). **L2 ✅ live-validated** chart 1.20.1 (server dry-run on real apiserver). |

### Untested code paths (real findings from reading source)

These have NO coverage at any layer today. Each is a candidate for
adding to L1 or L2 in a future PR — listed by code location for
bisect-friendliness:

- `src/webhook_server.py:199` — `cache lookup failed` error path on
  `ResourceOverrideCache.lookup` exception. Probably only firable when
  the watch thread crashes and the lock is left in an inconsistent state.
  Test: mock the cache to raise `RuntimeError` and assert webhook
  returns AdmissionReview with `allowed: false` + clear error message.
- `src/webhook_cache.py:142` — bootstrap `_initial_list` failure leads
  to `_log.exception` then the watch loop continues with an empty
  cache, silently passing admission for everything in opted-in
  namespaces. Test: mock `list_cluster_custom_object` to raise on
  first call, succeed on second; verify the cache is correctly
  populated by the retry.
- `src/webhook_rollout.py:284` — `ApiException` on the workload
  `patch_namespaced_*` call swallowed with a warning. Failure mode:
  rollout silently doesn't fire. Test: mock the apps client to return
  403 and assert the daemon logs `[rollout] patch denied` and DOES
  NOT crash the daemon thread.

## Tool prerequisites

```bash
# OS packages (Ubuntu/Debian; adapt for macOS):
sudo apt-get install -y docker.io
# kind
go install sigs.k8s.io/kind@latest
# kubectl + helm (use your package manager or:)
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
# Scanners (Phase 7)
sudo snap install kubeconform
go install github.com/aquasecurity/trivy/cmd/trivy@latest
pipx install pip-audit
```

---

## Phase 0 — ✅ Gap analysis (DONE)

Delivered as [`docs/config-interactions-audit.md`](docs/config-interactions-audit.md):
pairwise audit of every Config knob × marker annotation. 9 findings
(6 code-affecting + 3 docs-gap), all 9 resolved in chart 1.14.0 +
1.20.0 (~40 new QA asserts cover the resolved paths).

No commands to run — this phase is a deliverable, not a re-runnable
test.

---

## Phase 1 — Reproducible test cluster

Stand up a `kind` cluster with everything the integration tests need:
kube-prometheus-stack, ArgoCD, a local-mock GitLab.

```bash
# Creates a 3-node kind cluster on the kru-test name.
cat > /tmp/kind-config.yaml <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
EOF
kind create cluster --name kru-test --config /tmp/kind-config.yaml

# kube-prometheus-stack (provides Prometheus at
# http://prometheus-operated.monitoring.svc.cluster.local:9090).
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install kp prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set grafana.enabled=false \
  --set alertmanager.enabled=false \
  --wait

# ArgoCD (so Application + ResourceOverride syncs are reproducible).
helm repo add argo https://argoproj.github.io/argo-helm
helm install argocd argo/argo-cd \
  --namespace argocd --create-namespace --wait

# Local-mock GitLab — a tiny container that accepts /api/v4/projects/...
# requests and returns canned responses. Lets the integration tests
# exercise the MR-open code path without hitting a real GitLab.
docker run -d --name kru-mock-gitlab -p 8929:80 \
  ghcr.io/maxnordhog/mock-gitlab:latest  # placeholder; substitute or vendor

# Install the chart against the kind cluster, pointing at the local
# Prometheus + mock GitLab.
helm install kru charts/kube-resource-updater \
  --namespace kube-resource-updater --create-namespace \
  --set config.prometheusUrl=http://prometheus-operated.monitoring.svc.cluster.local:9090 \
  --set gitlab.token=mock-token \
  --set config.crWriteback.repoUrl=http://kru-mock-gitlab/infra/gitops.git \
  --set config.crWriteback.path=manifests/kube-resource-updater \
  --wait
```

**Tear-down:**

```bash
kind delete cluster --name kru-test
docker rm -f kru-mock-gitlab
```

---

## Phase 2 — Integration matrix (10 scenarios A–J)

Each scenario is an end-to-end run with a specific configuration.
Output asserted via `kubectl get`/`describe` + log greps. Bundle as
`tools/test-cluster/scenarios/<letter>.sh`; each script exits non-zero
on failure.

| ID | Scenario | Expected outcome |
|---|---|---|
| A | Bare opt-in workload end-to-end | Namespace annotated → CR written → pod admits with patched resources |
| B | OOM convergence with autoRollout | `polinux/stress` Deployment with 64Mi limit + 100M allocation → 5+ memory bumps over ~10 cron cycles, each followed by a rolling restart from auto-rollout |
| C | Mixed createMr bucket in same namespace | Workload A `createMr=true`, Workload B `createMr=false` → ONE direct-push commit + ONE MR opened in same sync |
| D | autoRollout debounce | 3 rapid CR changes within 30s → exactly 1 PodTemplate `restartedAt` stamp |
| E | Validating webhook rejection | `kubectl apply -f overlapping-cr.yaml` → admission denied with selector-overlap error |
| F | skipContainers excludes sidecar | Workload with `istio-proxy` sidecar + namespace `skipContainers: "istio-proxy"` → CR has 1 container entry, sidecar resources untouched |
| G | Hierarchy 3 levels for cpuPercentile | Helm 0.80 + ns 0.90 + workload 0.95 → effective Config = 0.95; remove workload annotation → 0.90; remove ns → 0.80 |
| H | oomFloorReset clears state | CR has `oom-floor.app: 256Mi`; annotate workload `oomFloorReset: "true"` → next sync drops all oom-* annotations |
| I | growOnly + shrinkOnly = freeze | Both flags true → CR identical to current apiserver state; OOM event in this state → `[oom-bump-suppressed]` warning, no bump applied |
| J | Selector fallback (no `app.kubernetes.io/*`) | Workload with only PodTemplate labels → CR selector uses `kubernetes.io/instance` or namespace-name fallback |

```bash
for scenario in A B C D E F G H I J; do
  bash tools/test-cluster/scenarios/$scenario.sh
done
```

---

## Phase 3 — Failure injection

Force a failure mode and confirm the tool degrades gracefully (no
silent data corruption, no crash loop without an actionable log).

| Failure | Injection | Expected behaviour |
|---|---|---|
| Prom 500 mid-sync | `kubectl patch deployment -n monitoring prometheus-operated -p '{"spec":{"replicas":0}}'` mid-sync | Tool logs `[prometheus] query failed` per container, continues with `None` values, opens MR with unchanged workloads filtered out |
| GitLab 503 on MR open | Mock GitLab returns 503 | Tool logs `[mr] retrying` ×3 then `[mr] giving up`; sync exits non-zero; partial state visible (commit pushed, MR not opened) |
| Watch drop mid-flight | `kubectl delete pod -n kube-resource-updater <webhook-pod>` | webhook_cache picks up where it left off via resourceVersion; no CR admission drops during the gap (failurePolicy: Ignore) |
| ConfigMap malformed YAML | Hand-edit ConfigMap, syntax-break a value | Pod fails `Config.load()` at startup with a clear error pointing at the bad key |
| Expired GITLAB_TOKEN | Replace Secret content with `expired-token` | Tool logs `[mr] 401 unauthorized` from the first MR-open attempt; sync exits non-zero |

---

## Phase 4 — Scale (50 workloads × 5 namespaces)

Generate 250 Deployments via a script + measure end-to-end:

```bash
bash tools/test-cluster/scale-load.sh   # creates 50 deploys × 5 ns
time kubectl create job -n kube-resource-updater \
  --from=cronjob/kube-resource-updater manual-scale
kubectl logs -n kube-resource-updater -l job-name=manual-scale -f > /tmp/scale.log
```

Targets:
- Sync duration < 60s
- Peak memory < 512MB (read from `kubectl top pod` during the run)
- MR description size < 1MB (GitLab body cap)
- Git clone + push < 10s combined

---

## Phase 5 — CRD schema validation

Apply intentionally-broken CRs and confirm the apiserver + validating
webhook reject them:

```bash
for bad in resources-wrapper.yaml missing-selector.yaml \
           missing-containers.yaml container-no-name.yaml \
           invalid-quantity.yaml; do
  kubectl apply -f tools/test-cluster/bad-cr/$bad 2>&1 | grep -q "denied"
  [ $? -eq 0 ] && echo "$bad: rejected (ok)" || echo "$bad: ACCEPTED (BUG)"
done
```

---

## Phase 6 — Upgrade path

Start on chart 1.11.0 (first slow-path-only release), generate state,
upgrade to current, confirm no regression:

```bash
# Install old version
helm install kru charts/kube-resource-updater \
  --version 1.11.0 \
  -f tools/test-cluster/upgrade-values.yaml \
  --namespace kube-resource-updater --create-namespace
# Run a sync to generate state (CRs, annotations)
kubectl create job -n kube-resource-updater \
  --from=cronjob/kube-resource-updater pre-upgrade
kubectl wait --for=condition=complete job/pre-upgrade -n kube-resource-updater
# Snapshot
kubectl get ro -A -o yaml > /tmp/pre-upgrade-crs.yaml
# Upgrade to current
helm upgrade kru charts/kube-resource-updater \
  -f tools/test-cluster/upgrade-values.yaml \
  --namespace kube-resource-updater
# Second sync
kubectl create job -n kube-resource-updater \
  --from=cronjob/kube-resource-updater post-upgrade
kubectl wait --for=condition=complete job/post-upgrade -n kube-resource-updater
# Compare
kubectl get ro -A -o yaml > /tmp/post-upgrade-crs.yaml
diff /tmp/pre-upgrade-crs.yaml /tmp/post-upgrade-crs.yaml | wc -l   # < 50 expected
```

Acceptance criteria:
- No spurious CR rewrites (re-render of unchanged workloads).
- Legacy CSV migration silent (no `[migration]` warnings on workloads
  that already had the new format).
- Per-workload `createMr` annotation starts being honored (chart 1.13.0+).
- `growOnly`/`shrinkOnly` start enforcing (chart 1.14.0+) without
  triggering a rollout storm.

---

## Phase 7 — ✅ OSS sanity (last run: chart 1.20.1, 2026-05-12)

Static analysis on the chart + image + Python deps + license
compatibility. No cluster needed. Run from the project root.

**Latest result summary:**

| Check | Status | Notes |
|---|---|---|
| 7.1 `helm lint` | ✅ | 0 chart(s) failed |
| 7.2 `kubeconform` rendered | ✅ | 19/19 resources schema-valid (CRD definition itself skipped — no schema for own CRD) |
| 7.3 `trivy image` | ✅ | **0 HIGH/CRITICAL fixable** (was 31 pre-1.20.1 — fixed by removing unused `helm` binary from Dockerfile and adding `apt-get upgrade` at build time, dropping ~21 Go-stdlib CVEs from the helm binary + 10 openssl/libssl CVEs from the base image) |
| 7.4 `pip-audit` | ✅ | 0 vulnerabilities |
| 7.5 license scan | ✅ | 25 deps: Apache-2.0, BSD-3-Clause, MIT, MPL-2.0, PSF — all compatible with Apache-2.0 project license. NO GPL/LGPL/AGPL. |

### 7.1 `helm lint`

```bash
helm dependency build charts/kube-resource-updater
helm lint charts/kube-resource-updater \
  --set config.crWriteback.repoUrl=https://example/x \
  --set config.crWriteback.path=overrides \
  --set config.prometheusUrl=http://prom:9090 \
  --set gitlab.token=qa
```

Expected: `0 chart(s) failed`.

### 7.2 `kubeconform` on rendered templates

```bash
helm template kru charts/kube-resource-updater \
  --set config.crWriteback.repoUrl=https://example/x \
  --set config.crWriteback.path=overrides \
  --set config.prometheusUrl=http://prom:9090 \
  --set gitlab.token=qa \
  | kubeconform -strict -summary -kubernetes-version 1.34.0 \
    -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{ .Group }}/{{ .ResourceKind }}_{{ .ResourceAPIVersion }}.json' \
    -schema-location default
```

CRD locations needed: `argoproj.io` (Application), `monitoring.coreos.com`
(ServiceMonitor), `kube-resource-updater.io` (ResourceOverride). Use
the in-tree CRD catalog above + a local schema for the custom CRD.

### 7.3 `trivy` image scan

```bash
trivy image --severity HIGH,CRITICAL \
  --ignore-unfixed \
  mateusgsilva95/kube-resource-updater:latest
```

Expected: no HIGH/CRITICAL with available fixes. Findings with no
upstream fix yet are documented in the release notes as "known CVEs
pending upstream patch".

### 7.4 `pip-audit` on deps

```bash
pip-audit --requirement requirements.txt --strict
```

Expected: 0 vulnerabilities, or each finding has a justification in
the release notes.

### 7.5 License compatibility scan

Walk the dependency graph and confirm every license is compatible
with Apache 2.0 (the project's license — see `LICENSE`):

```bash
pip install pip-licenses
pip-licenses --from=mixed --format=markdown \
  --packages $(pip show -f $(cat requirements.txt | cut -d= -f1) | grep '^Name:' | awk '{print $2}')
```

Expected: every license is one of Apache-2.0, MIT, BSD-2-Clause,
BSD-3-Clause, ISC, Python-2.0, MPL-2.0. **Reject:** GPL, AGPL, LGPL
(viral copyleft incompatible with Apache 2.0 distribution).

---

## Output convention

Every phase emits a log + a summary line. Aggregate runner:

```bash
bash tools/test-cluster/run-all.sh   # runs Phase 1 setup + 2-7 in order
# Produces tools/test-cluster/output/{phase}.log and a top-level
# tools/test-cluster/output/summary.md table:
#   | Phase | Status | Duration | Notes |
#   |---|---|---|---|
#   | 1 | ✅ | 4m12s | cluster up |
#   | 2 | ✅ | 8m05s | all 10 scenarios passed |
#   | 3 | ⚠️  | 6m31s | failure-inject E missing log line |
#   | ...
```

The runner returns 0 only when every phase is ✅. ⚠️ is acceptable for
documented known-issues; ❌ blocks the release.

---

## Maintenance

- **New scenario added in Phase 2?** Update the table here AND add the
  script under `tools/test-cluster/scenarios/`. The pattern is:
  setup → action → assert → cleanup.
- **New chart version that changes ConfigMap shape?** Add a row to
  Phase 6's upgrade matrix with the old → new transition you want
  validated.
- **New scanner in Phase 7?** Document the install command at the top
  of this file alongside the others.
