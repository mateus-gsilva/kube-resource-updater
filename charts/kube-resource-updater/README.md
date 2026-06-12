# kube-resource-updater (Helm chart)

Helm chart for the [kube-resource-updater](https://github.com/mateus-gsilva/kube-resource-updater) tool — Prometheus-driven, GitOps-friendly continuous resource right-sizing for Kubernetes workloads.

## What it installs

- **CronJob** that runs the sync loop on a schedule (default every 6h).
- **Mutation + Validation admission webhook** Deployment that patches pod resources at admission time.
- **`ResourceOverride` CRD** (`kube-resource-updater.io/v1`).
- All cluster-scoped RBAC + ClusterRoleBindings + ServiceAccount.
- Self-managed serving certs for the webhook (in-process reconciler — no cert-manager dependency).
- Optional `PodDisruptionBudget`, `ServiceMonitor`, `NetworkPolicy`, `extraDeploy`.

## Install

The chart is published to GHCR as an OCI artifact. The default
`image.repository` already points at the published image, so no image override
is needed.

```bash
helm install kube-resource-updater \
  oci://ghcr.io/mateus-gsilva/charts/kube-resource-updater --version 0.1.0 \
  --namespace kube-resource-updater --create-namespace \
  --values my-values.yaml
```

### Minimum `my-values.yaml`

```yaml
config:
  prometheusUrl: "http://prometheus-operated.monitoring.svc.cluster.local:9090"
  crWriteback:
    repoUrl: "https://gitlab.example.com/infra/cluster-gitops.git"
    branch: "main"
    path: "manifests/kube-resource-updater"

git:
  existingSecret: kube-resource-updater-git   # contains key 'token'
  # or inline: token: "<gitlab-or-github-token>"
```

The two `crWriteback.*` fields and a Prometheus URL are required at install time — the chart fails the render with a clear message if either is missing.

## Architecture

Two long-lived workloads share the same image, both configured by the same `ConfigMap`:

| Workload | Purpose |
|---|---|
| `CronJob` `kube-resource-updater` | Periodic sync: list opted-in namespaces, query Prometheus, compute recommendations, scan pod statuses for OOMKilled events, write CRs to git. Push direct OR open a Merge Request. |
| `Deployment` `kube-resource-updater-webhook` | Long-running: serves `MutatingAdmissionReview` (patches pod resources from matching `ResourceOverride` CRs) + `ValidatingAdmissionReview` (rejects selector-overlap conflicts). Also runs the in-process cert reconciler, the status writer, and the auto-rollout watcher. |

Opt-in is by Namespace annotation `kube-resource-updater.enabled: "true"`. Everything else (per-workload skip, autoRollout, OOM detection toggles, percentile / window / margin overrides) is also annotation-driven and follows the **workload > namespace > helm default** hierarchy.

## Key values

```yaml
# Sync cadence
cronjob:
  schedule: "0 */6 * * *"          # default 6h; crank to "*/10 * * * *" for OOM-heavy clusters

# Default sync behavior
config:
  dryRun: false                    # skip git writes (sync prints what it would write)
  createMr: true                   # false = push direct to crWriteback.branch
  cpuPercentile: "0.90"
  memPercentile: "0.90"
  cpuRequestWindow: "3d"
  memRequestWindow: "8d"
  marginFraction: "0.10"           # global fallback (10% headroom for scrape gaps + warmup peaks); per-type margins below override
  cpuRequestMargin: "0.20"         # +20% headroom over the CPU percentile
  memRequestMargin: "0.30"
  cpuLimitMultiplier: "4"          # fallback when Prometheus has no data
  memoryLimitMultiplier: "3"

# Floors / ceilings (0 = disabled)
  minCpuRequestM: "200"
  minMemoryRequestMi: "100"
  maxMemoryLimitMi: "0"            # also caps OOM bumps when > 0
  coldStartCpuFloorM: 10           # cold-start floor when Prom has no history — avoids the 1m throttle trap

# OOM-aware (chart 1.11.0+)
  oomDetectionEnabled: true
  oomBumpFactor: "1.5"             # ≥ 1.0 (clamped at load with warning)
  oomFloorEnabled: true            # false = bumps don't become sticky floors

# Webhook
webhook:
  enabled: true
  replicaCount: 1                  # single-runner-friendly; failurePolicy: Ignore absorbs restarts
  autoRollout:
    enabled: true                  # opt-in per-workload via annotation
    debounceSeconds: 30

# MR metadata (chart 1.7.0+)
config:
  mr:
    reviewers: "alice,bob"         # CSV usernames; resolved to IDs at sync time
    labels: "kube-resource-updater,automated"
    squash: true
```

See [`values.yaml`](values.yaml) for the full annotated list of knobs.

## Validating an install

```bash
# 1. Check the CronJob has the right env from the ConfigMap
kubectl -n kube-resource-updater describe cronjob kube-resource-updater | grep -A30 Environment

# 2. Trigger a one-off sync without waiting for the schedule
kubectl -n kube-resource-updater create job --from=cronjob/kube-resource-updater kube-resource-updater-manual
kubectl -n kube-resource-updater logs -l job-name=kube-resource-updater-manual -f

# 3. Confirm the webhook is admitting (look at one new pod in an opted-in ns)
kubectl get pod <pod> -o jsonpath='{.spec.containers[0].resources}'
```

## Versioning

Chart version mirrors the tool's `appVersion` (see [`Chart.yaml`](Chart.yaml)). If a GitOps engine (ArgoCD, Flux) references this chart pinned to a tag, bumps require updating that reference.

## More documentation

- Tool reference, annotations, OOM-aware bump algorithm, RBAC: [`docs/reference.md`](../../docs/reference.md)
- Architecture rationale + migration history: [`docs/webhook-migration.md`](../../docs/webhook-migration.md)
- Roadmap + release history: [`ROADMAP.md`](../../ROADMAP.md), [`CHANGELOG.md`](../../CHANGELOG.md)
