# kube-resource-updater — Reference

Reads real CPU and memory usage from Prometheus and writes resource
`requests` and `limits` back to Git for every ArgoCD-managed workload,
enabling GitOps-driven rightsizing without Kubernetes VPA.

---

## Table of contents

1. [Motivation](#motivation)
2. [End-to-end walkthrough](#end-to-end-walkthrough)
3. [Running locally](#running-locally)
4. [Deploying with Helm](#deploying-with-helm)
5. [How it works](#how-it-works)
6. [Sync flow](#sync-flow)
7. [Prometheus URL](#prometheus-url)
8. [Prometheus queries](#prometheus-queries)
9. [Resource computation](#resource-computation)
10. [Write-back targets](#write-back-targets)
11. [Git operations and MR creation](#git-operations-and-mr-creation)
12. [Annotations reference](#annotations-reference)
13. [Configuration reference](#configuration-reference)
14. [OOM-aware bumps](#oom-aware-bumps)
15. [Credential resolution](#credential-resolution)
15. [Effective config logging](#effective-config-logging)
16. [Commands](#commands)
17. [Kubernetes RBAC](#kubernetes-rbac)
18. [Compatibility](#compatibility)
19. [Helm chart reference](#helm-chart-reference)

---

## Motivation

### Inspired by ArgoCD Image Updater

[ArgoCD Image Updater](https://argocd-image-updater.readthedocs.io/) watches container
registries, detects new image tags, and writes them back to the git repository that
ArgoCD watches — opening a Merge Request so a human approves the bump before it goes live.

kube-resource-updater applies the same pattern to resource sizing: instead of watching
a registry, it watches Prometheus; instead of proposing an image tag update, it proposes
CPU and memory `requests` and `limits`. The result is a MR that shows exactly what changed
and by how much, waiting for approval before ArgoCD syncs it to the cluster.

### Why not Kubernetes VPA?

Vertical Pod Autoscaler is the Kubernetes-native solution for rightsizing, but it has
fundamental incompatibilities with GitOps:

| | VPA | kube-resource-updater |
|---|---|---|
| **Where recommendations land** | Directly in the running pod (mutating webhook or in-place update) | In git, as a Merge Request |
| **ArgoCD compatibility** | Works, but requires `ignoreDifferences` on every Application for resource fields — git drifts from cluster state | Native: git is always the source of truth |
| **Approval workflow** | None — changes applied immediately (Auto/Recreate mode) | MR opens for human review; owner approves, rejects, or adjusts |
| **Git as source of truth** | No — VPA writes directly to pods; git never reflects actual values | Yes — recommendations land in git before reaching the cluster |
| **Pod disruption** | Evicts pods immediately and randomly when the recommender decides (Recreate mode) | Rolling update triggered by ArgoCD after MR is approved and merged — same controlled process as any other deploy |
| **Audit trail** | VPA recommendation objects in etcd; no git history | Git commits with before/after values; full history, reviewable, revertable |
| **Works with HPA** | Conflicts on CPU (VPA and HPA both adjust CPU independently) | Passive; writes to git, does not scale pods — coexists with HPA |
| **Extra components** | VPA CRDs + recommender + admission webhook (cluster-wide) | Single CronJob + ServiceAccount; no CRDs, no webhooks |

VPA objects can be managed by ArgoCD like any other resource. However, VPA
recommendations are stored in `VerticalPodAutoscaler` status fields inside the cluster
— ArgoCD has no mechanism to read them and write values back to git. `updateMode: Off`
is the only safe GitOps mode; tools like
[Goldilocks](https://github.com/FairwindsOps/goldilocks) surface those recommendations
in a dashboard but still require a human to manually copy values to git.
kube-resource-updater skips the VPA CRD entirely and queries Prometheus directly,
closing the automation gap.

### Key benefits

- **MR-first by default** — every sync cycle opens one MR per repository with a full
  diff table: container, before/after CPU+memory, and percentage delta. Nothing reaches
  the cluster without passing through git review.
- **GitOps-native** — recommendations land in the same files ArgoCD already watches;
  no drift between git and cluster state.
- **Uses your existing observability stack** — no new metrics, no new collectors.
  Queries the same Prometheus that backs your dashboards.
- **Configurable per workload** — percentile, window, margin, grow-only, shrink-only
  overridable per Application via annotations without touching the global config.
- **Controlled rollout** — no surprise evictions; the rolling update happens when ArgoCD
  syncs after the MR is merged, going through the same controlled deployment process as
  any other change.
- **Pipeline-compatible** — since changes land as a Merge Request, CI pipelines run
  against the proposed values before they reach the cluster (lint, policy checks,
  cost estimation, Slack notifications, etc.).
- **Multi-cluster** — one CronJob instance manages resources across all ArgoCD-registered
  clusters simultaneously.

---

## End-to-end walkthrough

This section shows the complete flow from opting a namespace in to the pods being admitted with the new resources.

### Step 1 — Opt the Namespace in

```bash
kubectl annotate namespace my-app kube-resource-updater.enabled=true
```

That's the only required annotation. Everything else (skip a specific workload, change percentile per namespace, enable autoRollout, turn off OOM detection) is also annotation-driven — see [Annotations reference](#annotations-reference).

The webhook short-circuits admission for namespaces without this annotation; the sync ignores them entirely. Workloads in opted-in namespaces are filtered further by their PodTemplate labels — they must have at least one of `app.kubernetes.io/instance` / `app.kubernetes.io/name` / `app.kubernetes.io/component` so the tool can derive a stable CR selector.

### Step 2 — The CronJob fires

On the configured schedule (`0 */6 * * *` by default), one Job spawns inside the release namespace and runs `python main.py sync`. Logs produced:

```
INFO  [git] using GITLAB_TOKEN
INFO  [config] cpu: p=90%  req-win=3d  lim-win=7d  margin=20%  floor=200m  ceil=off  mult=4.0x
INFO  [config] mem: p=90%  req-win=8d  lim-win=7d  margin=30%  floor=100Mi  ceil=off  mult=3.0x
INFO  [prometheus] using http://prometheus-operated.monitoring.svc.cluster.local:9090 (k8s-service-dns)
INFO
INFO  === my-app ===  (workloads=2)
INFO    my-app-api  containers=1
INFO    my-app-worker  containers=2
INFO  [oom] detected 1 fresh OOM event(s) across 1 workload(s); memory will be bumped where dedupe says new.
INFO    [oom-bump] my-app/my-app-worker/worker: 256Mi → 384Mi (trap=256Mi, ×1.5)
INFO  [OK] my-app: manifests/kube-resource-updater/my-app.resource-override.yaml
INFO    my-app-api/api  req=cpu:280m (+18%) mem:412Mi (+12%)  lim=cpu:1120m (+20%) mem:1236Mi
INFO    my-app-worker/worker  req=cpu:120m (+5%) mem:128Mi  lim=cpu:480m mem:384Mi (+50%)
INFO    my-app-worker/exporter  req=cpu:50m mem:64Mi  lim=cpu:200m mem:192Mi
INFO  [OK] webhook: MR opened https://gitlab.example.com/infra/cluster-gitops/-/merge_requests/42 (branch resource-updater/sync → main)
INFO
INFO  MR: https://gitlab.example.com/infra/cluster-gitops/-/merge_requests/42
INFO  -> namespaces: my-app
```

Key lines:
- `[config] cpu / mem` — effective defaults after annotations resolve
- `[oom-bump]` — fresh OOMKilled was detected; this sync bumps the limit
- `[OK] my-app: ...resource-override.yaml` — one CR file per namespace; one document per workload inside
- `MR: ...` — the GitLab MR URL (or `Pushed:` URL when `createMr: false`)

### Step 3 — The CR file

The sync writes (or updates) one file per opted-in namespace under `<crWriteback.path>/`:

```yaml
# manifests/kube-resource-updater/my-app.resource-override.yaml
apiVersion: kube-resource-updater.io/v1
kind: ResourceOverride
metadata:
  name: my-app-api
  namespace: my-app
  labels:
    app.kubernetes.io/managed-by: kube-resource-updater
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: my-app-api
  containers:
    - name: api
      requests: { cpu: 280m, memory: 412Mi }
      limits:   { cpu: 1120m, memory: 1236Mi }
---
apiVersion: kube-resource-updater.io/v1
kind: ResourceOverride
metadata:
  name: my-app-worker
  namespace: my-app
  labels:
    app.kubernetes.io/managed-by: kube-resource-updater
  annotations:
    kube-resource-updater.io/oom-floor.worker: 384Mi
    kube-resource-updater.io/oom-last-event.worker: "2026-05-09T17:42:11Z"
    kube-resource-updater.io/oom-boost-history.worker: |-
      2026-05-09T17:42:11Z 256Mi→384Mi (×1.5)
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: my-app-worker
  containers:
    - name: worker
      requests: { cpu: 120m, memory: 128Mi }
      limits:   { cpu: 480m, memory: 384Mi }
    - name: exporter
      requests: { cpu: 50m, memory: 64Mi }
      limits:   { cpu: 200m, memory: 192Mi }
```

One CR per workload, identified by `metadata.name == workload-name`. Container resources land flat under `spec.containers[i].{requests,limits}` (no `resources:` wrapper — the CRD schema rejects it). OOM-aware annotations are per-container.

### Step 4 — The Merge Request

If `config.createMr: true` (the default), one MR is opened per repo with a summary of every container's diff:

---

**chore(resources): update 2 ResourceOverride(s) across 1 namespace(s)**

## Resource Recommendations

| Namespace | Workload | Container | CPU req | Mem req | CPU limit | Mem limit |
|---|---|---|---|---|---|---|
| `my-app` | `my-app-api` | `api` | `280m` +18% | `412Mi` +12% | `1120m` +20% | `1236Mi` |
| `my-app` | `my-app-worker` | `worker` | `120m` +5% | `128Mi` | `480m` | `384Mi` +50% |
| `my-app` | `my-app-worker` | `exporter` | `50m` | `64Mi` | `200m` | `192Mi` |

> Requests: Prometheus percentile × (1 + margin). Limits: Prometheus max × (1 + margin). Fallback multipliers: CPU ×4, memory ×3.

---

The percentage (`+X%` / `-X%`) is the delta vs the previous CR content in git. When `createMr: false`, the same diff goes through `git push origin <branch>` directly — no MR, no review.

### Step 5 — Review, merge, admit

After merge, ArgoCD/Flux applies the CR to the cluster. Two things happen next:

1. **New pods admit through the webhook** — `/mutate-pod` looks up CRs in the pod's namespace whose `selector.matchLabels` matches the pod's labels, then patches `pod.spec.containers[*].resources` for any matching container name. Pods admitted before the CR change keep the old resources until they restart.

2. **Auto-rollout (if enabled)** — when the CR changes and the workload has `kube-resource-updater.autoRollout: "true"` (or it's set on the namespace), the webhook stamps `kubectl.kubernetes.io/restartedAt: <now>` on the workload's PodTemplate. The kubelet sees the template hash change and rolls the pods with the new resources within seconds.

For workloads without autoRollout, the new resources take effect on the next natural rollout (chart upgrade, image change, manual `kubectl rollout restart`).

---

## Running locally

This tutorial is for local development and testing only. In production the tool runs
as a CronJob inside the cluster — see [Deploying with Helm](#deploying-with-helm).

### Prerequisites

- Python 3.11+
- `kubectl` configured with a context pointing to the cluster where ArgoCD runs
- Read access to the ArgoCD namespace (to list `Application` and `Secret` objects)
- A GitLab token with `read_repository` + `write_repository` scope (or `api`)

### 1 — Install dependencies

```bash
cd kube-resource-updater
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2 — Create the .env file

```bash
# .env — never commit this file
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GITLAB_USERNAME=your-username

# Prometheus: point to a reachable instance
# Option A: port-forward in-cluster Prometheus
#   kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090
PROMETHEUS_URL_IN_CLUSTER=http://localhost:9090

# Option B: use an external Prometheus URL directly
# PROMETHEUS_URL_STAGING_CLUSTER=https://prometheus-stg.example.com

# ArgoCD namespace (skip if it's "argocd")
# ARGOCD_NAMESPACE=argocd

# Safety: skip all git/MR operations
DRY_RUN=true
CREATE_MR=false
```

Minimum required: `GITLAB_TOKEN` + at least one `PROMETHEUS_URL_*` for each cluster
that has enabled apps.

### 3 — Port-forward Prometheus (if in-cluster)

```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090 &
```

Verify it's reachable:
```bash
curl -s http://localhost:9090/api/v1/status/buildinfo | python3 -m json.tool | grep version
```

### 4 — Check Prometheus connectivity

```bash
set -a && source .env && set +a
.venv/bin/python3 main.py check-prometheus
```

Expected output:
```
INFO [check] in-cluster: OK (version: 2.45.0)
```

If you see `UNREACHABLE`, fix the URL before running sync.

### 5 — Run a dry-run sync

```bash
set -a && source .env && set +a
DRY_RUN=true .venv/bin/python3 main.py sync --mr
```

Check for:
- No tracebacks
- Apps discovered and listed
- Workloads found (not all skipped)
- Resource values printed (not all `n/a`)
- No git operations performed (`DRY_RUN=true` in logs)

### 6 — Run the QA script

```bash
set -a && source .env && set +a
.venv/bin/python3 tools/qa_params.py
```

The QA output must show differentiated values across workloads and reasonable
`lim/req` ratios (≥ 2× for CPU, ≥ 1.2× for memory).

---

## Deploying with Helm

### Prerequisites

- Helm 3.10+
- `kubectl` access to the ArgoCD cluster
- A GitLab token stored as a Kubernetes Secret (see step 1)
- The Helm chart is bundled in the repository under `chart/`

### 1 — Create the GitLab token secret

```bash
kubectl create secret generic kube-resource-updater-gitlab-token \
  -n argocd \
  --from-literal=token=glpat-xxxxxxxxxxxxxxxxxxxx
```

### 2 — Create your values file

```yaml
# my-values.yaml
cronjob:
  schedule: "0 */6 * * *"
  timeZone: "UTC"

config:
  dryRun: false
  createMr: true
  cpuPercentile: "0.90"
  memPercentile: "0.90"
  marginFraction: "0.20"

# Per-cluster Prometheus URLs (if auto-discovery is not sufficient)
clusters:
  staging-cluster:
    prometheusUrl: "https://prometheus-stg.example.com"
  prod-cluster:
    prometheusUrl: "https://prometheus-prd.example.com"

gitlab:
  existingSecret: kube-resource-updater-gitlab-token
  existingSecretKey: token

nodeSelector:
  kubernetes.io/os: linux
```

The `config:` section and `clusters:` map are rendered into a ConfigMap mounted at
`/etc/kube-resource-updater/config.yaml`. Only `GITLAB_TOKEN` stays as an env var
(from the `gitlab.existingSecret`).

### 3 — Install

```bash
helm upgrade --install kube-resource-updater ./chart \
  -n argocd \
  --create-namespace \
  -f my-values.yaml
```

Verify the CronJob was created:
```bash
kubectl get cronjob -n argocd kube-resource-updater
```

### 4 — Trigger a manual run

```bash
kubectl create job -n argocd \
  --from=cronjob/kube-resource-updater \
  kube-resource-updater-manual

kubectl logs -n argocd \
  -l job-name=kube-resource-updater-manual -f
```

### 5 — Verify discovery is working

In the logs, confirm that Prometheus was discovered for each cluster:
```
INFO [prometheus] in-cluster: http://prometheus-operated.monitoring.svc.cluster.local:9090 (k8s-service-dns, auto-detected, healthy)
```

If you see `no URL configured or discovered`, add the URL via `prometheusUrls` in
your values file (see [Prometheus URL](#prometheus-url)).

---

## How it works

The chart deploys two long-lived workloads inside the cluster, both reading the same mounted `ConfigMap`:

1. **Sync `CronJob`** (`python main.py sync`) — fires on the configured schedule. Lists every `Namespace` that carries `kube-resource-updater.enabled: "true"`, lists every `Deployment` / `StatefulSet` inside those namespaces, queries Prometheus for actual usage, scans `pod.status.containerStatuses[*]` for OOMKilled events, computes recommendations, and writes one `ResourceOverride` Custom Resource per workload to a Git repo (direct push or Merge Request).

2. **Admission webhook `Deployment`** (`python main.py webhook`) — serves three endpoints:
   - `POST /mutate-pod` — looks up matching `ResourceOverride` CRs by `selector.matchLabels` ∩ `pod.metadata.labels`; patches `pod.spec.containers[*].resources` for any matching container name. Powered by an in-memory CR cache fed by a `watch.list_cluster_custom_object` informer; admission stays under the 5s kube-apiserver timeout regardless of CR count.
   - `POST /validate-resourceoverride` — rejects new CRs whose selector overlaps an existing CR in the same namespace AND shares a container name (would result in non-deterministic admission patches).
   - `GET /readyz` / `GET /healthz` / `GET /metrics` — for kubelet + Prometheus.

The webhook process also runs three daemon threads: the in-process cert reconciler (generates + rotates a self-signed CA + serving cert, patches the `caBundle` on the MWC/VWC), the status writer (stamps `lastAppliedAt` on CRs that admit pods), and the auto-rollout watcher (stamps `restartedAt` on workloads whose CR resources changed, when the workload's `autoRollout` resolves true).

The CRD (`kube-resource-updater.io/v1` `ResourceOverride`) is the single, uniform write target — independent of whether the underlying app ships as Helm, Kustomize, plain manifests, or a private chart. The only external runtime dependency is **Prometheus** (kube-state-metrics + cAdvisor metrics, standard with kube-prometheus-stack).

---

## Sync flow

```
CronJob fires
│
├─ load_config()                      ← /etc/kube-resource-updater/config.yaml
│
├─ resolve_prometheus_url()           ← see Prometheus discovery
│
├─ list_enabled_namespaces()
│    Lists every Namespace with kube-resource-updater.enabled: "true"
│
├─ list_workloads(opt-in namespaces)
│    Deployments + StatefulSets in those namespaces only
│
├─ resolve_for_workload(rec) per workload
│    Applies the helm < ns < workload hierarchy to produce an effective Config
│    Filters: skip / grow+shrink conflict / no containers / etc.
│
├─ OOM-aware
│    fetch_oom_state(opt-in namespaces)
│      Lists CRs via apiserver, reads oom-floor / oom-last-event /
│      oom-boost-history annotations (source of truth, not the git clone).
│    detect_oom_events(opt-in namespaces, workload_keys)
│      Lists pods, scans containerStatuses[*].lastState.terminated.reason
│      Returns the most-recent OOMKilled event per (ns, workload, container)
│      with finished_at + trap_limit_bytes.
│
├─ prefetch_prometheus_parallel()     ← 4 queries per container, parallel
│
├─ write_back_webhook_all(workloads, oom_state, oom_events, ...)
│    Build phase (no I/O):
│      _build_entries → _build_containers_payload per workload
│      Computes res from Prom values, applies _enforce_floors,
│      _apply_grow_shrink, _apply_oom_floor (bump + sticky).
│      Returns one WebhookEntry per workload (selector + containers
│      + per-container OOM annotations to stamp).
│    Commit phase:
│      Clone gitops repo at crWriteback.branch
│      _read_old_docs (deltas for MR description + carry-forward)
│      _write_namespace_files (one file per namespace, multi-doc YAML)
│      _prune_orphan_files (drop files for namespaces no longer opted in)
│      git commit → either push direct OR push branch + open/update MR.
│
└─ summary log: MR URL(s) or Pushed URL, namespaces touched.
```

One git clone + one commit per `crWriteback.repoUrl`, regardless of how many namespaces produced changes.

---

## Prometheus URL

Set `config.prometheusUrl` explicitly in the chart values — auto-discovery
was removed The chart's `templates/validate.yaml` aborts
`helm install/upgrade` when the field is empty AND `cronjob.enabled: true`
(webhook-only installs don't need a URL since admission requests carry
the Pod inline). At runtime, `Config.validate` mirrors the same check
and exits with code 2 before any sync work runs.

The kube-prometheus-stack convention is:

```yaml
config:
  prometheusUrl: "http://prometheus-operated.monitoring.svc.cluster.local:9090"
```

Adjust the Service name / namespace if Prometheus lives elsewhere (e.g. a
non-`monitoring` namespace, or behind an ingress for cross-cluster reads).

### Fallback when no data

When the URL is reachable but a query returns no data or errors, a
`WARNING` is emitted per container per value:

```
WARNING [prometheus] no CPU request data for <ns>/<container> (url: <url>)
WARNING [prometheus] no memory request data for <ns>/<container> (url: <url>)
WARNING [prometheus] no CPU limit data for <ns>/<container> (url: <url>)
WARNING [prometheus] no memory limit data for <ns>/<container> (url: <url>)
```

| Condition | Result |
|---|---|
| `prometheusUrl` empty | `helm install` fails (or `Config.validate` exits 2 at runtime) |
| URL set, host unreachable | Per-container `WARNING` + that value is `None` (sync continues, no recommendation for that container) |
| Query returns no data | `WARNING` per container; that value is `None` |
| Query exception (timeout, HTTP error) | `WARNING` per container; that value is `None` |

---

## Prometheus queries

All queries are issued against `/api/v1/query` (instant query at current time).
Results are cached in-process per `(url, namespace, container, workload, window/percentile)`
tuple for the duration of the sync — repeated calls within the same run are free.

### CPU request

Estimates the steady-state CPU need at a configurable percentile over a sliding window.

```promql
quantile_over_time(
  <CPU_PERCENTILE>,
  max without(pod)(
    rate(container_cpu_usage_seconds_total{
      namespace="<ns>",
      container="<container>",
      pod=~"<workload>-.*"   ← only when workload name is known
    }[1m])
  )[<CPU_REQUEST_WINDOW>:1m]
)
```

**Why `max without(pod)` before the quantile:** collapses all pods of a workload into a
single time series (the max CPU across replicas at each minute) before taking the
percentile. This makes the recommendation independent of replica count and immune to
outlier pods from short-lived rollouts.

**Default:** p90 over 3 days (`CPU_PERCENTILE=0.90`, `CPU_REQUEST_WINDOW=3d`)

### Memory request

```promql
quantile_over_time(
  <MEM_PERCENTILE>,
  max without(pod)(
    container_memory_working_set_bytes{
      namespace="<ns>",
      container="<container>",
      pod=~"<workload>-.*"
    }
  )[<MEM_REQUEST_WINDOW>:5m]
)
```

Uses `container_memory_working_set_bytes` — the same metric the kubelet uses for OOM
eviction decisions — not `container_memory_usage_bytes` (which includes cache).

**Default:** p90 over 8 days (`MEM_PERCENTILE=0.90`, `MEM_REQUEST_WINDOW=8d`)
A longer window than CPU because memory profiles are stickier and weekly patterns matter.

### CPU limit

Captures the worst-case instantaneous spike, not a smooth average:

```promql
max_over_time(
  irate(container_cpu_usage_seconds_total{
    namespace="<ns>",
    container="<container>",
    pod=~"<workload>-.*"
  }[5m])
  [<CPU_LIMIT_WINDOW>:15s]
)
```

`irate[5m]` with a 15-second subquery step catches short startup bursts that `rate[1m]`
would smooth away. `max_over_time` ensures the limit accommodates the historical maximum.

**Default:** `CPU_LIMIT_WINDOW=7d`

### Memory limit

```promql
max_over_time(
  container_memory_working_set_bytes{
    namespace="<ns>",
    container="<container>",
    pod=~"<workload>-.*"
  }[<MEM_LIMIT_WINDOW>]
)
```

**Default:** `MEM_LIMIT_WINDOW=7d`

---

## Resource computation

### Step 1 — Raw Prometheus values

Four values are fetched per container: `cpu_req_raw`, `mem_req_raw`, `cpu_lim_raw`, `mem_lim_raw`.

### Step 2 — Apply margin

Each type has an independent margin. If a per-type margin is not set, falls back to
`MARGIN_FRACTION` (default `0.00` — no automatic margin).

```
cpu_req  = cpu_req_raw  × (1 + effective_cpu_request_margin)
mem_req  = mem_req_raw  × (1 + effective_mem_request_margin)
cpu_lim  = cpu_lim_raw  × (1 + effective_cpu_limit_margin)
mem_lim  = mem_lim_raw  × (1 + effective_mem_limit_margin)
```

### Step 3 — Fallback to multiplier

When Prometheus has no data for a value (returns `None`), limits fall back to:

```
cpu_lim  = cpu_req × CPU_LIMIT_MULTIPLIER   (default: 4×)
mem_lim  = mem_req × MEMORY_LIMIT_MULTIPLIER (default: 3×)
```

If requests are also `None`, the existing values in the YAML are preserved unchanged.

### Step 4 — Apply floors and ceilings

| Variable | Type | Default | Effect |
|---|---|---|---|
| `MIN_CPU_REQUEST_M` | floor | 0 (disabled) | `cpu_req = max(cpu_req, N)` |
| `MIN_MEMORY_REQUEST_MI` | floor | 0 (disabled) | `mem_req = max(mem_req, N MiB)` |
| `MAX_CPU_REQUEST_M` | ceiling | 0 (disabled) | `cpu_req = min(cpu_req, N)` |
| `MAX_MEMORY_REQUEST_MI` | ceiling | 0 (disabled) | `mem_req = min(mem_req, N MiB)` |
| `MAX_CPU_LIMIT_M` | ceiling | 0 (disabled) | `cpu_lim = min(cpu_lim, N)` |
| `MAX_MEMORY_LIMIT_MI` | ceiling | 0 (disabled) | `mem_lim = min(mem_lim, N MiB)` |
| `MIN_CPU_LIMIT_M` | floor (global) | 0 (disabled) | Applied after all per-type computation |
| `MIN_MEMORY_LIMIT_MI` | floor (global) | 0 (disabled) | Applied after all per-type computation |

### Step 5 — Rounding (optional)

When `ROUND_VALUES=true`, each value is rounded up to the nearest
order-of-magnitude step: `1–9 → 10`, `10–99 → 100 step 10`, `100–999 → 100 step 100`, etc.
Example: `101m → 200m`, `11Mi → 20Mi`.

Default: disabled.

### Step 6 — Grow-only / Shrink-only modes

These modes clamp the computed value against the **apiserver-source CR's containers** (what new pods admit with right now). Compared to absolute floors/ceilings (`minCpu*` / `maxCpu*`), these are velocity bounds: "only allow growth" or "only allow shrinkage" relative to the workload's current state.

Configurable globally (chart values), per-namespace, or per-workload (annotation). Hierarchy: **workload > namespace > helm default**.

**Global defaults** — apply to every workload that doesn't override:

| Env var | Helm key | Default | Effect |
|---|---|---|---|
| `GROW_ONLY` | `config.growOnly` | `false` | Only increase values globally |
| `SHRINK_ONLY` | `config.shrinkOnly` | `false` | Only decrease values globally |

**Per-namespace / per-workload annotation** — overrides the global for that scope:

```yaml
metadata:
  annotations:
    kube-resource-updater.growOnly: "true"     # only grow this workload
    # or
    kube-resource-updater.shrinkOnly: "true"   # only shrink this workload
```

**Behaviour summary** (with workload-level set):

| Workload annotation | Helm default | Effective mode |
|---|---|---|
| `growOnly` | shrinkOnly | growOnly (workload wins) |
| `shrinkOnly` | growOnly | shrinkOnly (workload wins) |
| none | shrinkOnly | shrinkOnly (inherited) |
| none | growOnly | growOnly (inherited) |
| none | none | normal — always overwrites |

Modes apply independently to each container, request and limit dimension.

**Operation order** — relative to OOM-aware and bounds:

```
1. compute_from_prom()        — raw recommendation
2. apply_oom_bump()            — if fresh OOMKilled event detected
3. apply_sticky_floor()        — prior OOM floor
4. apply_grow_shrink(old_res)  — operator POLICY clamp vs apiserver-source CR
5. enforce_floors_and_ceilings — hard INVARIANTS always win (min*/max*)
```

> **Interaction with OOM-aware:** `shrinkOnly` is **absolute** — even when the slow-path detects a fresh OOMKilled event, the bump is computed but reverted by `shrinkOnly` (operator policy beats kernel signal, the tool doesn't second-guess). The pod will continue OOMing. A clear WARNING is logged:
>
> ```
> [oom-bump-suppressed] my-app/api/main: shrinkOnly clamped bump 200Mi → 300Mi back to 200Mi;
>     pod will continue OOMing until operator removes shrinkOnly OR sets
>     oomDetectionEnabled=false on this workload.
> ```
>
> When `oom-bump-suppressed` fires, the `oom-floor.<container>` annotation is NOT stamped (truthful — floor reflects what was applied, not what was attempted), but `oom-last-event.<container>` advances so dedupe doesn't re-process the same event next sync.
>
> `growOnly` has no conflict — bump grows, growOnly allows growth.

> **Both flags active = "freeze":** the workload is held at its current CR values; OOM bumps are suppressed by `shrinkOnly`; only floors/ceilings can still change values. Logged loudly per sync:
>
> ```
> [freeze] my-app/api: growOnly + shrinkOnly both active — workload frozen at current CR values
>     (only floors/ceilings can still change values; OOM bumps will be suppressed by shrinkOnly
>     with a separate warning). Remove one flag to resume normal operation.
> ```

> **First sync for a workload:** the apiserver-source CR's `spec.containers` is the comparison baseline for grow/shrink. On the very FIRST sync for a workload (no CR exists yet), grow/shrink no-op — there's nothing to compare against. Subsequent syncs use the previously-written CR as the baseline.

---

## Write-back target

The tool writes a single target: one `ResourceOverride` CR per workload, grouped per-namespace into one multi-doc YAML file inside the cluster's gitops repo. There are no Helm-value-tree or Kustomize-patch write-back modes anymore — the CR + admission webhook combination decouples the resource override from the underlying app's structure.

### File layout

```
<crWriteback.repoUrl>@<crWriteback.branch>:
  <crWriteback.path>/
    <namespace-1>.resource-override.yaml    ← one file per opted-in namespace
    <namespace-2>.resource-override.yaml
    ...
```

The three `crWriteback.*` values are chart values; the chart `required` template fails the install when `repoUrl` or `path` is empty.

> **Changing `crWriteback.path` after first sync leaves orphans.** The orphan-cleanup pass only scans inside the configured `crWriteback.path` — it doesn't know the old path. Files written under the previous path stay in the repo and (if the ArgoCD Application's `directory.recurse: true`) still get applied as CRs in the cluster, including stale ones for workloads that no longer opt in. Mitigation: when you change the path, manually `git rm -r <old-path>/` in the gitops repo before the next sync. The tool will not detect or warn about this — moving the path is a deliberate operator action and recursive-scanning the whole repo for orphan CR-shaped files would be expensive and risky.

> **Two workloads with the same name in the same namespace** (e.g. `Deployment/foo` + `StatefulSet/foo`) get their CR names auto-disambiguated with a kind prefix: `deployment-foo` + `statefulset-foo`. The tool logs a WARNING with the rename so operators with selectors hardcoded against the bare CR name can update. To avoid the prefix, rename one of the colliding workloads. Non-colliding workloads keep their bare workload name as `cr_name`.

### One file, one document per workload

```yaml
# manifests/kube-resource-updater/my-app.resource-override.yaml
apiVersion: kube-resource-updater.io/v1
kind: ResourceOverride
metadata:
  name: my-app-api          # == workload name (Deployment / StatefulSet)
  namespace: my-app
  labels:
    app.kubernetes.io/managed-by: kube-resource-updater
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: my-app-api
  containers:
    - name: api
      requests: { cpu: 280m, memory: 412Mi }
      limits:   { cpu: 1120m, memory: 1236Mi }
---
apiVersion: kube-resource-updater.io/v1
kind: ResourceOverride
metadata:
  name: my-app-worker
  namespace: my-app
  ...
```

The selector is derived from the workload's PodTemplate labels in priority order: `app.kubernetes.io/instance` → `app.kubernetes.io/name` → `app.kubernetes.io/component`. Loki specifically motivated using all three: its chart deploys four StatefulSets sharing `instance` and `name`, and only `component` disambiguates them. A workload missing all three falls back to `app.kubernetes.io/name: <target-name>` — in which case the deployment must also carry that label, otherwise the webhook never matches.

Container resources land **flat** under `spec.containers[i].{requests,limits}` — no `resources:` wrapper (the CRD's OpenAPI v3 schema rejects it).

### Orphan cleanup

When a namespace stops opting in (`kube-resource-updater.enabled: "true"` removed), the next sync detects the orphan file under `crWriteback.path` and `git rm`s it as part of the same commit. The pruning only touches files matching `<namespace>.resource-override.yaml` — operator-added YAMLs in the same directory survive.

### Push direct vs Merge Request

`config.createMr` (default `true`) selects between:
- **`createMr: true`** — push to branch `resource-updater/sync`, open or update one MR per repo via the GitLab REST API. MR metadata (`assignees` / `reviewers` / `labels` / `squash`) comes from `config.mr.*`.
- **`createMr: false`** — push direct to `crWriteback.branch`. Useful when the operator wants OOM-aware bumps to apply without manual review gates (the ROADMAP "Split MR vs direct push by urgency" item proposes per-diff routing as a less-invasive default).

---

## Git operations and MR creation

### Clone

The repository is cloned with `--depth=1` on the branch configured via `config.crWriteback.branch` (default `main`).

### Commit

Author name and email are configurable via `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL`.

Commit message format:
```
chore(resources): update resource requests/limits

[one line per workload: container → cpu_req / mem_req / cpu_lim / mem_lim]
```

### Push vs MR

When `CREATE_MR=true` (default):
- Changes are pushed to a new branch `resource-updater/sync`
- A GitLab Merge Request is opened with a full diff table showing before/after values
  and percentage deltas for every container

When `CREATE_MR=false`:
- Changes are pushed directly to the target branch

**MR deduplication:** if an MR from `resource-updater/sync` to the target branch
already exists, the existing MR is reused (HTTP 409 → list and return the existing URL).

**Repository grouping:** applications sharing the same `repoURL + targetRevision` are
batched into a single git clone + commit + push, producing one MR per repository
regardless of how many Applications reference it.

### Dry run

When `DRY_RUN=true`, all Prometheus queries, resource computation, and YAML patching
run normally, but no git operations are performed. Computed values are logged.

---

## Annotations reference

> The tool runs in **webhook + `ResourceOverride` CRD** mode. Annotations are set on **`Namespace`** (opt-in marker — required) and optionally on individual workloads (`Deployment` / `StatefulSet`) to override defaults for that workload only. All keys use the prefix `kube-resource-updater.` and camelCase value names matching `values.yaml`. The full source of truth is [`src/overrides.py`](../src/overrides.py) (`_KEY_SPEC`).

### Markers (no Config field; behavior-only)

| Annotation | Scope | Description |
|---|---|---|
| `kube-resource-updater.enabled` | Namespace only | **Required**. `"true"` opts the namespace into discovery; everything in this namespace is then evaluated. |
| `kube-resource-updater.skip` | Workload only | `"true"` excludes this specific workload from sync + webhook patches. **Important:** if the workload previously had a CR managed by the tool, setting `skip` causes the next sync to REMOVE the CR from the file → ArgoCD prunes the CR → admission webhook stops patching → new pods admit with deployment-spec resources (whatever `resources:` is in the chart/manifest). If you want to keep the existing CR frozen instead, use `growOnly: "true"` + `shrinkOnly: "true"` together (freeze semantics). |
| `kube-resource-updater.skipContainers` | Namespace OR workload | CSV of container names to leave alone (e.g. `"istio-proxy,linkerd-proxy"`). Init containers are always skipped automatically. An explicit empty string at workload-level clears an inherited namespace list. |
| `kube-resource-updater.autoRollout` | Namespace OR workload | `"true"` makes the webhook stamp `kubectl.kubernetes.io/restartedAt` on the workload's PodTemplate when its CR changes, triggering a rolling restart. Hierarchy: workload > namespace > helm default. |
| `kube-resource-updater.oomDetectionEnabled` | Namespace OR workload | Opt-out of OOM-aware bumps for a specific workload (default `true` via helm). |
| `kube-resource-updater.oomFloorEnabled` | Namespace OR workload | Makes OOM bumps **sticky** via `oom-floor.<container>` annotation on the CR (default `true`). False = one-shot bumps; the limit goes up this sync but no floor is recorded, so the next Prom-driven recommendation can drop the limit again. |
| `kube-resource-updater.oomFloorReset` | Namespace OR workload | One-shot opt-in. `"true"` causes the next sync to clear `oom-floor.<c>` + `oom-last-event.<c>` + `oom-boost-history.<c>` for every container of every CR in scope. The tool does NOT auto-remove this annotation — operator deletes it manually after confirming the reset took effect. |
| `kube-resource-updater.createMr` | Namespace OR workload | `"true"` (default) opens a Merge Request for this workload's diff; `"false"` pushes direct to the target branch; resolution follows the workload > namespace > helm hierarchy. A single sync can produce two parallel pushes per repo: one direct (workloads with `createMr=false`), one MR (workloads with `createMr=true`). |

### Config overrides (anything from the chart's `config:` block)

Every key in `config:` of [`values.yaml`](../charts/kube-resource-updater/values.yaml) is overridable per-namespace and per-workload via `kube-resource-updater.<key>` (camelCase preserved). Resolution order: **workload > namespace > helm default**. Common overrides:

| Annotation | Type | Description |
|---|---|---|
| `kube-resource-updater.cpuPercentile` | float | e.g. `"0.95"` — percentile for CPU request estimation |
| `kube-resource-updater.memPercentile` | float | Percentile for memory request estimation |
| `kube-resource-updater.cpuRequestWindow` | duration | Prometheus lookback, e.g. `"5d"` |
| `kube-resource-updater.memRequestWindow` | duration | Same for memory request |
| `kube-resource-updater.marginFraction` | float | `"0.20"` = +20% headroom over the percentile |
| `kube-resource-updater.cpuLimitMultiplier` | float | Fallback: limit = request × N when no Prometheus data |
| `kube-resource-updater.memoryLimitMultiplier` | float | Same for memory |
| `kube-resource-updater.minCpuRequestM` | int | Floor in millicores |
| `kube-resource-updater.minMemoryRequestMi` | int | Floor in MiB |
| `kube-resource-updater.maxMemoryLimitMi` | int | Hard ceiling, also caps OOM bumps |
| `kube-resource-updater.coldStartCpuFloorM` | int | Cold-start CPU floor (millicores) for a freshly-OOMed container with no Prometheus history; avoids the sub-10m CFS throttle trap. Default `10` |
| `kube-resource-updater.growOnly` | bool | Only allow resources to increase this sync |
| `kube-resource-updater.shrinkOnly` | bool | Only allow resources to decrease |
| `kube-resource-updater.oomBumpFactor` | float | Multiplier applied to the limit at OOM time, default `"1.5"` |

Unknown keys log a typo warning at sync time listing the known set. A malformed value (e.g. `oomBumpFactor: "yes"`) falls through to the next layer of the hierarchy rather than crashing.

---

## Configuration reference

Configuration is loaded from a ConfigMap mounted at
`/etc/kube-resource-updater/config.yaml`. The Helm chart renders this file from
`values.yaml`; only `GITLAB_TOKEN` stays as an environment variable (from the
`gitlab.existingSecret`).

**Config file structure:**

```yaml
config:            # global defaults
  dryRun: false
  createMr: true
  cpuPercentile: "0.90"
  ...

clusters:          # per-cluster overrides (only prometheusUrl for now)
  staging-cluster:
    prometheusUrl: "https://prometheus-stg.example.com"
  prod-cluster:
    prometheusUrl: "https://prometheus-prd.example.com"
```

> **Local development fallback:** when no config file is found, the tool falls back to
> environment variables with a deprecation warning. Env var names are listed below each
> table entry. This fallback will be removed in a future major release.

### Git / GitLab

| Config key | Env var (deprecated) | Default | Description |
|---|---|---|---|
| — | `GITLAB_TOKEN` | `""` | GitLab token — always read from env var, never in the ConfigMap |
| — | `GITLAB_URL` | `""` | GitLab base URL — always read from env var |
| `gitlabUsername` | `GITLAB_USERNAME` | `""` | Username for token auth (user-generated tokens) |
| `gitAuthorName` | `GIT_AUTHOR_NAME` | `kube-resource-updater` | Git commit author name |
| `gitAuthorEmail` | `GIT_AUTHOR_EMAIL` | `kube-resource-updater@cluster.local` | Git commit author email |
| `createMr` | `CREATE_MR` | `true` | `true` opens a GitLab MR; `false` pushes directly |
| `dryRun` | `DRY_RUN` | `false` | `true` skips all git operations |

### ArgoCD

| Config key | Env var (deprecated) | Default | Description |
|---|---|---|---|
| `argoCdNamespace` | `ARGOCD_NAMESPACE` | auto-detected | ArgoCD namespace. Auto-detected from the pod's service account token mount when unset. |

### Prometheus

| Config key | Env var (deprecated) | Default | Description |
|---|---|---|---|
| `clusters.<name>.prometheusUrl` | `PROMETHEUS_URL_<CLUSTER>` | `""` | Per-cluster Prometheus URL |
| `prometheusUrl` | `PROMETHEUS_URL` | `""` | Default URL used when no per-cluster URL is set |
| `resourceSource` | `RESOURCE_SOURCE` | `prometheus` | Resource source. Only `prometheus` is supported. |

### Query parameters

| Config key | Env var (deprecated) | Default | Description |
|---|---|---|---|
| `cpuPercentile` | `CPU_PERCENTILE` | `0.90` | Percentile for CPU request estimation (`0.0`–`1.0`) |
| `memPercentile` | `MEM_PERCENTILE` | `0.90` | Percentile for memory request estimation |
| `cpuRequestWindow` | `CPU_REQUEST_WINDOW` | `3d` | Lookback window for CPU request percentile query |
| `memRequestWindow` | `MEM_REQUEST_WINDOW` | `8d` | Lookback window for memory request percentile query |
| `cpuLimitWindow` | `CPU_LIMIT_WINDOW` | `7d` | Lookback window for CPU limit max query |
| `memLimitWindow` | `MEM_LIMIT_WINDOW` | `7d` | Lookback window for memory limit max query |

### Margins

All margins are fractions (e.g. `0.20` = +20%). Per-type margins fall back to
`marginFraction` when unset.

| Config key | Env var (deprecated) | Default | Description |
|---|---|---|---|
| `marginFraction` | `MARGIN_FRACTION` | `0.00` | Global fallback margin for all types |
| `cpuRequestMargin` | `CPU_REQUEST_MARGIN` | unset | Margin added on top of CPU request raw value |
| `memRequestMargin` | `MEM_REQUEST_MARGIN` | unset | Margin added on top of memory request raw value |
| `cpuLimitMargin` | `CPU_LIMIT_MARGIN` | unset | Margin added on top of CPU limit raw value |
| `memLimitMargin` | `MEM_LIMIT_MARGIN` | unset | Margin added on top of memory limit raw value |

### Bounds

`0` disables the bound.

| Config key | Env var (deprecated) | Default | Description |
|---|---|---|---|
| `minCpuRequestM` | `MIN_CPU_REQUEST_M` | `0` | Minimum CPU request in millicores |
| `minMemoryRequestMi` | `MIN_MEMORY_REQUEST_MI` | `0` | Minimum memory request in MiB |
| `maxCpuRequestM` | `MAX_CPU_REQUEST_M` | `0` | Maximum CPU request in millicores |
| `maxMemoryRequestMi` | `MAX_MEMORY_REQUEST_MI` | `0` | Maximum memory request in MiB |
| `maxCpuLimitM` | `MAX_CPU_LIMIT_M` | `0` | Maximum CPU limit in millicores |
| `maxMemoryLimitMi` | `MAX_MEMORY_LIMIT_MI` | `0` | Maximum memory limit in MiB |
| `minCpuLimitM` | `MIN_CPU_LIMIT_M` | `0` | Global minimum CPU limit in millicores |
| `minMemoryLimitMi` | `MIN_MEMORY_LIMIT_MI` | `0` | Global minimum memory limit in MiB |

### Fallback multipliers (no Prometheus data)

| Config key | Env var (deprecated) | Default | Description |
|---|---|---|---|
| `cpuLimitMultiplier` | `CPU_LIMIT_MULTIPLIER` | `4` | CPU limit = CPU request × N when no Prometheus data |
| `memoryLimitMultiplier` | `MEMORY_LIMIT_MULTIPLIER` | `3` | Memory limit = memory request × N when no Prometheus data |

### OOM-aware

Slow-path scans `pod.status.containerStatuses[*].lastState.terminated.reason == "OOMKilled"` at sync time and bumps memory limits on opted-in workloads. Fully covered below in [OOM-aware bumps](#oom-aware-bumps).

| Config key | Env var | Default | Description |
|---|---|---|---|
| `oomDetectionEnabled` | `OOM_DETECTION_ENABLED` | `true` | Enable OOM scan + bump. Set false to disable detection entirely. Per-workload override via annotation. |
| `oomBumpFactor` | `OOM_BUMP_FACTOR` | `1.5` | New limit = `pod.spec.limits.memory × bumpFactor`. Clamped to `≥ 1.0` at load with warning (operator footgun guard). |
| `oomFloorEnabled` | `OOM_FLOOR_ENABLED` | `true` | Make bumps **sticky** via `oom-floor.<container>` annotation on the CR. False = one-shot help; the limit goes up this sync but no floor recorded. Per-workload override. |

The companion **`kube-resource-updater.oomFloorReset`** annotation (no helm default — explicit opt-in only) clears all OOM state for the next sync. See [Annotations reference](#annotations-reference).

### Other

| Config key | Env var (deprecated) | Default | Description |
|---|---|---|---|
| `growOnly` | `GROW_ONLY` | `false` | Global grow-only mode (see Step 6) |
| `shrinkOnly` | `SHRINK_ONLY` | `false` | Global shrink-only mode (see Step 6) |
| `roundValues` | `ROUND_VALUES` | `false` | Round computed values up to nearest order-of-magnitude step |
| `logLevel` | `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `logFormat` | `LOG_FORMAT` | `text` | `text` for human-readable, `json` for structured (Loki-compatible) |

---

## OOM-aware bumps

Workloads that OOM at limit `X` will OOM again at `X` after restart if the recommendation stays at `X`. The Prometheus history is **capped at `X`** (kubelet kills before the working_set can grow past the limit), so a percentile-based recommendation never escapes — the loop is structural, not statistical.

The slow-path OOM-aware path breaks the loop by reading pod status directly at sync time and stamping a per-container **floor** annotation that subsequent syncs respect.

### Detection

`cmd_sync` lists pods in every opted-in namespace and scans `containerStatuses[*].lastState.terminated.reason == "OOMKilled"`. For every match, it produces an `OomEvent`:

```python
OomEvent(
    namespace="<ns>",
    workload_name="<deployment-or-statefulset>",  # ownerReferences chain
    container="<container-name>",
    finished_at="2026-05-09T18:05:11Z",            # RFC3339Z, dedupe key
    trap_limit_bytes=104857600,                    # the limit kernel killed at
)
```

Workload identity follows `ownerReferences`: a pod from a `ReplicaSet` strips the pod-template-hash to recover the `Deployment` name; `StatefulSet` / `DaemonSet` owners are used directly. Pods with no recognized owner are skipped silently.

### Bump

When a fresh OOM is detected (`finished_at > prior_last_event[c]`), `_build_containers_payload` applies:

```
bump_target = trap_limit_bytes × oomBumpFactor
new_limit   = max(prom_recommendation, bump_target)
new_limit   = min(new_limit, max_memory_limit_mi × MiB)   # if maxMemoryLimitMi > 0
```

The request scales proportionally so `lim / req` stays at `memoryLimitMultiplier` (typically 3×). `_enforce_floors` runs once before and once after the bump.

### Per-container annotations stamped on the CR

| Key | Format | Meaning |
|---|---|---|
| `kube-resource-updater.io/oom-floor.<container>` | quantity (e.g. `760Mi`) | Sticky minimum. Every subsequent sync clamps memory request/limit UP to this value. Only written when `oomFloorEnabled` resolves true. |
| `kube-resource-updater.io/oom-last-event.<container>` | RFC3339Z | Durable dedupe — the bump fires only when a more-recent OOM `finishedAt` is observed. |
| `kube-resource-updater.io/oom-boost-history.<container>` | multi-line, FIFO 10 | Audit trail. Each line: `<RFC3339Z> <from>→<to> (×<factor>)`. Older entries roll off. |

### Convergence example

Pod with `limits.memory: 16Mi` allocating 600 MiB, `min_memory_request_mi: 100Mi`, `oomBumpFactor: 1.5`:

```
Sync 1   trap=16Mi   bump=24Mi   limit=100Mi   (covered by floor; logged as [oom-noop])
Sync 2   trap=100Mi  bump=150Mi  limit=150Mi   floor=150Mi
Sync 3   trap=150Mi  bump=225Mi  limit=225Mi   floor=225Mi
Sync 4   trap=225Mi  bump=338Mi  limit=338Mi   floor=338Mi
Sync 5   trap=338Mi  bump=507Mi  limit=507Mi   floor=507Mi
Sync 6   trap=507Mi  bump=760Mi  limit=760Mi   floor=760Mi   ← converged, no further OOM
```

End-to-end latency per iteration: `cronjob.schedule + MR merge + ArgoCD sync + autoRollout`. Default 6h cron → days to converge with manual merge. Operators on OOM-heavy clusters set `cronjob.schedule: "*/10 * * * *"` and configure GitLab auto-merge (or set `createMr: false` to push direct).

### No-Prom + fresh OOM (newly-deployed workloads)

A brand-new workload has no Prometheus history. Without special handling, `_build_container_resources` returns empty for that container and the sync skips it — the CR is never written, the workload OOMs forever waiting for Prometheus data.

The slow-path detects this case and **synthesizes** a minimal `requests/limits` from the OOM trap + floors:

```
mem_req = max(min_memory_request_mi × MiB, 1)
mem_lim = max(trap_limit_bytes, mem_req)        # holds lim ≥ req invariant
```

Then the bump path runs normally — the workload escapes the OOM loop on the first sync that detects an event.

### Operator controls

- **`kube-resource-updater.oomDetectionEnabled: "false"`** — turn off OOM scanning for one workload that does its own memory management.
- **`kube-resource-updater.oomFloorEnabled: "false"`** — keep detection on, but make bumps one-shot. Useful when Prometheus is the long-term source of truth and a sticky floor would block sizing-down after a code optimization.
- **`kube-resource-updater.oomFloorReset: "true"`** — clear accumulated floor state once (e.g. after the workload was optimized). The tool does NOT auto-remove this annotation; the operator deletes it manually after confirming the reset took effect, otherwise every sync re-resets and blocks future bumps from accumulating.

---

## Credential resolution

Git credentials come from the **`GITLAB_TOKEN`** env var only (the chart populates it from `gitlab.token` or `gitlab.existingSecret`). The ArgoCD repo-Secret fallback that earlier versions supported was removed — a single source kept the matrix of "where did the token actually come from?" small.

Auth format used for `git clone`:

```
https://<GITLAB_USERNAME>:<GITLAB_TOKEN>@<host>/<path>.git
```

`GITLAB_USERNAME` defaults to `oauth2` when unset (the right value for GitLab project access tokens). For user-generated personal access tokens, set `gitlabUsername` in chart values to the token-owner's username.

The active credential is logged once at startup:

```
INFO [git] using GITLAB_TOKEN
WARNING [git] GITLAB_TOKEN not set — git operations will fail (set gitlab.existingSecret or gitlab.token in chart values)
```

The token needs `read_repository` + `write_repository` scope on the cluster gitops repo (or `api` for the MR-opening path).

---

## Effective config logging

At startup, global configuration is logged in two compact lines plus an optional flags line:

```
INFO [config] cpu: p=80%  req-win=3d  lim-win=7d  margin=20%  floor=200m  ceil=off  mult=4.0x
INFO [config] mem: p=80%  req-win=3d  lim-win=7d  margin=20%  floor=100Mi  ceil=off  mult=3.0x
INFO [config] flags: createMr=false  dryRun=true
```

The flags line is omitted when all flags are at their defaults (`growOnly=false shrinkOnly=false dryRun=false createMr=true roundValues=false`).

Per workload, any field whose effective value differs from the helm default is logged immediately after the workload discovery line:

```
=== my-app ===  (workloads=2)
  my-app-api  containers=1
    my-app/my-app-api [override]: cpuPercentile=95%  marginFraction=30%
  my-app-worker  containers=2
    my-app/my-app-worker [override]: growOnly=True  oomFloorEnabled=False
```

The source layer is approximate (namespace vs. workload annotation) and grouped under `[override]`. Workloads whose effective config matches the helm defaults produce no `[override]` line.

---

## Commands

### `sync`

Main sync command. Discovers apps, queries Prometheus, and writes back resources.

```bash
python main.py sync [--mr | --no-mr]
```

`--mr` / `--no-mr` overrides the `CREATE_MR` env var for this run.

### `check-prometheus`

Probes all discovered and configured Prometheus URLs and reports their status.
Exits with code `1` if any URL is unreachable.

```bash
python main.py check-prometheus
```

Output example:
```
INFO [check] ops-cluster: OK (version: 2.45.0)
INFO [check] staging-cluster: OK (version: 2.45.0)
WARNING [check] prod-cluster: UNREACHABLE: ...
```

---

## Kubernetes RBAC

The chart renders two ClusterRoles + ClusterRoleBindings, both bound to ServiceAccounts in the release namespace.

### CronJob ClusterRole (`kube-resource-updater`)

Always granted:

| Resource | Verbs | Reason |
|---|---|---|
| `namespaces` | `get`, `list` | Discover opted-in namespaces |
| `deployments.apps`, `statefulsets.apps` | `get`, `list` | Workload discovery in opted-in namespaces |

Gated on `config.oomDetectionEnabled` (default true):

| Resource | Verbs | Reason |
|---|---|---|
| `pods` | `get`, `list` | Scan `containerStatuses[*].lastState.terminated.reason` at sync time |
| `resourceoverrides.kube-resource-updater.io` | `get`, `list` | Read live `oom-floor.<container>` / `oom-last-event.<container>` annotations as source of truth |

**No discovery-mode grants**: Prometheus auto-discovery was
removed along with the three cluster-wide reads it required (`endpoints`,
`services`, `prometheuses.monitoring.coreos.com`). The operator sets
`config.prometheusUrl` explicitly; the chart fails `helm install` when
it's empty.

### Webhook ClusterRole (`kube-resource-updater-webhook`)

| Resource | Verbs | Reason |
|---|---|---|
| `namespaces` | `get`, `list`, `watch` | Cache opt-in markers, short-circuit admission for non-opted-in namespaces |
| `resourceoverrides.kube-resource-updater.io` | `get`, `list`, `watch` | Informer that feeds `/mutate-pod` and `/validate-resourceoverride` |
| `resourceoverrides.kube-resource-updater.io/status` | `patch` | Stamp `lastAppliedAt` |
| `mutatingwebhookconfigurations`, `validatingwebhookconfigurations` | `get`, `patch` | In-process cert reconciler patches `clientConfig.caBundle` on the chart's own MWC/VWC objects |
| `deployments.apps`, `statefulsets.apps` | `get`, `list`, `watch` | Auto-rollout watcher (gated on `webhook.autoRollout.enabled`) |
| `deployments.apps`, `statefulsets.apps` | `patch` | Stamp `restartedAt` on the workload's PodTemplate when its CR changes (gated on `webhook.autoRollout.enabled`) |

---

## Compatibility

Minimum and tested versions for each external dependency the chart and tool
talk to. "Minimum" = the lowest version where every feature this chart uses
is available; "Tested" = what the project's live validation cluster runs
against today.

| Dependency | Minimum | Tested | Why |
|---|---|---|---|
| **Kubernetes** | 1.27 | 1.34 | `CronJob.spec.timeZone` is GA in 1.27 (Beta in 1.25, default-off; not safe to require pre-1.27). `ServerSideApply=true` in `Application.spec.syncPolicy.syncOptions` requires server-side apply (GA in 1.22 — covered by the floor). Admission webhooks use `admissionregistration.k8s.io/v1` (GA 1.16). |
| **Argo CD** | 2.6 | 2.13 | Project consumes `argoproj.io/v1alpha1` `Application` resources. The GitLab SCM webhook integration (`gitlab-scm-token` secret in the ArgoCD namespace) is supported from 2.5+; project's live test relies on it for auto-sync without manual `argocd app sync`. |
| **Helm** | 3.10 | 3.16 | Chart uses Helm v2 API (`apiVersion: v2` in Chart.yaml), Bitnami `common` chart as a dependency (2.x.x), and the `required` / `fail` template functions for fail-fast validation. All available in Helm 3.10+. |
| **GitLab** | 14.0 | self-hosted (current) | MR creation / lookup goes through `/api/v4/projects/:id/merge_requests` and `/api/v4/users` (REST v4, stable since GitLab 10.x). Squash-merge + assignee/reviewer arrays are accepted as documented in 14.0+. |
| **Prometheus** | 2.30 | 2.x (kube-prometheus-stack) | Tool queries `/api/v1/query_range` and uses `quantile_over_time(...)` aggregation, both stable since 2.x. No PromQL features specific to newer 2.4x+ are used. |
| **Python (build-time)** | 3.10 | 3.12 | The image's base interpreter. `match` statements and PEP 604 unions (`int \| None`) are required by the code; both land in 3.10. |
| **kubernetes (Python client)** | 29.0 | 30.x | `client.CustomObjectsApi.patch_namespaced_custom_object` + `apply_namespaced_custom_object` semantics + the watch reconnect-friendly behavior the cache relies on. Pinned in `requirements.txt`. |

**What an operator needs to verify before deploying:** the cluster runs
Kubernetes ≥ 1.27 (mostly — clouds tend to be 1.28+ now), the GitOps engine
can reconcile a chart with `argoproj.io/v1alpha1` Applications **or** is
applied with plain `helm install` / `kubectl apply`, and either a Prometheus
URL is configured explicitly or the in-cluster auto-discovery finds a
reachable instance.

**What's NOT a dependency:**
  - **cert-manager** — dropped The webhook now owns its own
    serving cert through an in-process reconciler.
  - **VPA** — removed in 1.0.0. The tool never read VerticalPodAutoscaler
    objects; it always read raw Prometheus.
  - **MetricsServer** — only Prometheus is queried for usage data.

---

## Helm chart reference

The tool is deployed via a Helm chart as a `CronJob`.

### Minimum values to override

```yaml
cronjob:
  schedule: "0 */6 * * *"
  timeZone: "America/Sao_Paulo"

config:
  cpuPercentile: "0.80"
  memPercentile: "0.80"
  marginFraction: "0.20"

gitlab:
  existingSecret: my-gitlab-token-secret
  existingSecretKey: token

nodeSelector:
  kubernetes.io/os: linux
```

### Prometheus URL configuration

Single-cluster only. `config.prometheusUrl`
is **required** — the helm install fails when it's empty AND
`cronjob.enabled: true`. The kube-prometheus-stack convention is:

```yaml
config:
  prometheusUrl: "http://prometheus-operated.monitoring.svc.cluster.local:9090"
```

Auto-discovery (Service-DNS + Prometheus CR `externalUrl` fallback) was dropped
in 1.20.0; see the [Prometheus URL](#prometheus-url) section above for the
rationale.

### CronJob parameters

| Parameter | Default | Description |
|---|---|---|
| `cronjob.schedule` | `0 */6 * * *` | Cron expression |
| `cronjob.timeZone` | `UTC` | Timezone for the schedule |
| `cronjob.concurrencyPolicy` | `Forbid` | Prevent overlapping runs |
| `cronjob.backoffLimit` | `2` | Retries before marking job failed |
| `cronjob.activeDeadlineSeconds` | `1800` | Kill the job after 30 min |
| `cronjob.ttlSecondsAfterFinished` | `3600` | Clean up completed jobs after 1h |
| `cronjob.restartPolicy` | `OnFailure` | Pod restart policy |

### Triggering a manual run

```bash
kubectl create job -n kube-resource-updater \
  --from=cronjob/kube-resource-updater \
  kube-resource-updater-manual

kubectl logs -n kube-resource-updater \
  -l job-name=kube-resource-updater-manual -f
```

### Alerting (PrometheusRule)

The chart ships a `PrometheusRule` (requires prometheus-operator / kube-prometheus-stack).
It exists because the `MutatingWebhookConfiguration` runs `failurePolicy: Ignore`: when the
webhook is down the apiserver silently bypasses it and pods admit with their chart-default
`resources:` instead of the kube-resource-updater recommendation. That bypass went undetected
for ~15h in the 2026-05-28 outage. Enable it:

```yaml
webhook:
  metrics:
    serviceMonitor:
      enabled: true        # required — the up/error alerts depend on the scrape working
      labels:
        release: kp        # match your kube-prometheus-stack release name
    prometheusRule:
      enabled: true
      labels:
        release: kp        # must match kube-prometheus-stack's ruleSelector
```

Confirm pickup:

```bash
kubectl get prometheusrule -n kube-resource-updater \
  -o jsonpath='{.items[0].spec.groups[0].rules[*].alert}'
```

#### The alerts

| Alert | Severity | for | Signal |
|---|---|---|---|
| `KubeResourceUpdaterWebhookDown` | critical | 2m | `up{job=<webhook>,namespace=<ns>} == 0` — Prometheus can no longer scrape the webhook |
| `KubeResourceUpdaterWebhookFailingOpen` | critical | 0m | `increase(apiserver_admission_webhook_fail_open_count{name="pods.kube-resource-updater.io",type="admit"}[5m]) > 0` — the apiserver is admitting pods without the webhook *right now* |
| `KubeResourceUpdaterValidatingWebhookFailingOpen` | warning | 0m | same counter, `type="validating"` — only rendered when `webhook.validating.enabled=true` |
| `KubeResourceUpdaterWebhookErrors` | warning | 5m | `rate(resource_updater_webhook_admission_errors_total[5m]) > 0` — degraded but not yet failing open |

`WebhookDown` is the upstream signal (pod gone); `WebhookFailingOpen` is the confirmation
straight from the apiserver's own counter (the definitive "patches are being skipped" signal).
`apiserver_admission_webhook_fail_open_count` is exported by OKE's managed control plane
under the default `job="apiserver"` scrape — no extra config (verified on OKE).

If kube-prometheus-stack's `ruleNamespaceSelector` watches a different namespace (e.g.
`monitoring`), set `webhook.metrics.prometheusRule.namespace` accordingly (mirrors
`serviceMonitor.namespace`).

---

## JSON log fields reference

When `LOG_FORMAT=json`, every log line is a JSON object. The baseline fields
(`timestamp`, `level`, `logger`, `message`) appear on every line. The
formatter additionally surfaces two computed fields when applicable —
both let Loki / ELK / Splunk consumers filter on event categories without
regex-parsing the message body:

| Field | When emitted | Value |
|---|---|---|
| `tag`   | Any line whose message starts with `[<word>]` (e.g. `[OK]`, `[oom-bump]`, `[DRY RUN]`). | The bracketed token without brackets — `"OK"`, `"oom-bump"`, `"DRY RUN"`. The message body keeps the bracketed form. |
| `phase` | Any line emitted inside a `phase_ctx(...)` block in `main.cmd_sync`. | One of `"discovery"`, `"recommend"`, `"result"`. |

Per-event `extra={...}` fields:

| Event | Extra fields |
|---|---|
| Prometheus discovered | `prometheus_url`, `discovery_method` |
| Namespace listed for sync | `namespace`, `workload_count` |
| Workload skipped | `namespace`, `workload`, `status: "skip"`, `reason` |
| Workload delta line | `namespace`, `workload`, `container`, `req_cpu`, `req_mem`, `lim_cpu`, `lim_mem` |
| Namespace CR file acknowledged | `namespace`, `cr_path`, `status: "ok"` |
| OOM event detected | `namespace`, `workload`, `container`, `finished_at`, `trap_limit_bytes` |
| MR opened | `mr_url`, `source_branch`, `target_branch`, `namespaces`, `mode: "mr"` |
| Direct push | `files_changed`, `files_deleted`, `namespaces`, `repo_url`, `branch`, `mode: "direct"` |
| MR metadata | `assignees`, `reviewers`, `labels`, `squash` |
| Sync summary | `namespace_count`, `workload_count` |

Example Loki queries:

```logql
# All workloads skipped this sync (with the reason)
{job="kube-resource-updater"} | json | status="skip"

# Which discovery method is being used for Prometheus
{job="kube-resource-updater"} | json | discovery_method!=""

# All MR URLs opened
{job="kube-resource-updater"} | json | tag="OK" | mode="mr"

# All OOM bumps applied
{job="kube-resource-updater"} | json | tag="oom-bump"

# Per-workload delta on a specific container
{job="kube-resource-updater"} | json
  | namespace="my-app" | container="cache"
  | line_format "{{.req_cpu}} cpu, {{.req_mem}} mem"

# All lines emitted during the recommend phase
{job="kube-resource-updater"} | json | phase="recommend"
```
