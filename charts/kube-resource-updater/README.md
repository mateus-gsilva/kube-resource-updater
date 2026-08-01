# kube-resource-updater

Prometheus-driven, GitOps-friendly continuous resource right-sizing for Kubernetes workloads. The chart deploys a scheduled **sync CronJob** that turns Prometheus usage history into `ResourceOverride` CRs committed back to Git, and an in-cluster **mutating admission webhook** that applies those CRs to pods at admission time — so it works with any GitOps engine (Argo CD, Flux, plain kubectl).

## TL;DR

```console
helm install kube-resource-updater \
  oci://ghcr.io/mateus-gsilva/charts/kube-resource-updater \
  --namespace kube-resource-updater --create-namespace \
  --set config.prometheusUrl=http://prometheus-operated.monitoring.svc.cluster.local:9090 \
  --set config.crWriteback.repoUrl=https://gitlab.example.com/infra/cluster-gitops.git \
  --set config.crWriteback.path=manifests/kube-resource-updater \
  --set git.token=<git-token>
```

## Introduction

This chart bootstraps a [kube-resource-updater](https://github.com/mateus-gsilva/kube-resource-updater) deployment on a Kubernetes cluster using the [Helm](https://helm.sh) package manager. It installs:

- A **CronJob** that runs the sync loop on a schedule (default every 6h): lists opted-in namespaces, queries Prometheus, computes CPU/memory recommendations, detects `OOMKilled` events, and writes `ResourceOverride` CRs to Git (direct push or Merge Request).
- A **mutating + validating admission webhook** Deployment that patches pod resources from matching CRs at admission time and rejects conflicting CRs. It manages its own serving certificate in-process — no cert-manager dependency.
- The **`ResourceOverride` CRD** (`kube-resource-updater.io/v1`).
- All cluster-scoped RBAC, bindings, and a ServiceAccount.
- Optional `PodDisruptionBudget`, `ServiceMonitor`, `PrometheusRule`, `NetworkPolicy`, and `extraDeploy` resources.

## Prerequisites

- Kubernetes 1.27+
- Helm 3.8.0+ (OCI registry support)
- A reachable Prometheus endpoint with workload CPU/memory history
- A Git repository and token where `ResourceOverride` CRs are committed

## Installing the Chart

The chart is published to GHCR as an OCI artifact; the default `image.repository` already points at the published image, so no image override is needed.

```console
helm install kube-resource-updater \
  oci://ghcr.io/mateus-gsilva/charts/kube-resource-updater --version 0.1.5 \
  --namespace kube-resource-updater --create-namespace \
  --values my-values.yaml
```

Minimum `my-values.yaml`:

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

`config.prometheusUrl` and both `config.crWriteback.{repoUrl,path}` are required when the CronJob is enabled — the chart fails the render with a clear message if any is missing.

## Uninstalling the Chart

```console
helm uninstall kube-resource-updater --namespace kube-resource-updater
```

This removes all chart-managed resources. The `ResourceOverride` CRD and any CRs are intentionally left in place; delete them manually if you no longer need them:

```console
kubectl delete crd resourceoverrides.kube-resource-updater.io
```

## Architecture

Two long-lived workloads share the same image and `ConfigMap`:

| Workload | Purpose |
| --- | --- |
| `CronJob` `kube-resource-updater` | Periodic sync: list opted-in namespaces, query Prometheus, compute recommendations, scan pod statuses for OOMKilled events, write CRs to Git (direct push or Merge Request). |
| `Deployment` `kube-resource-updater-webhook` | Long-running: serves `MutatingAdmissionReview` (patches pod resources from matching `ResourceOverride` CRs) + `ValidatingAdmissionReview` (rejects selector-overlap conflicts). Also runs the in-process cert reconciler, the status writer, and the auto-rollout watcher. |

Opt-in is by Namespace annotation `kube-resource-updater.enabled: "true"`. Everything else (per-workload skip, autoRollout, OOM detection toggles, percentile / window / margin overrides) is annotation-driven and follows the **workload > namespace > Helm default** hierarchy.

## Parameters

### Global parameters

| Name | Description | Value |
| ---- | ----------- | ----- |
| `global.imageRegistry` | Global container image registry applied to all images | `""` |
| `global.imagePullSecrets` | Global container registry secret names as an array | `[]` |
| `kubeVersion` | Override Kubernetes version | `""` |
| `nameOverride` | Override the chart name | `""` |
| `fullnameOverride` | Override the full release name | `""` |
| `commonLabels` | Labels added to all resources | `{}` |
| `commonAnnotations` | Annotations added to all resources | `{}` |
| `clusterDomain` | Kubernetes cluster domain | `cluster.local` |
| `extraDeploy` | Extra manifests to deploy | `[]` |

### Diagnostic mode

| Name | Description | Value |
| ---- | ----------- | ----- |
| `diagnosticMode.enabled` | Run the container in sleep mode for debugging | `false` |
| `diagnosticMode.command` | Command override in diagnostic mode | `["sleep"]` |
| `diagnosticMode.args` | Args override in diagnostic mode | `["infinity"]` |

### Image

| Name | Description | Value |
| ---- | ----------- | ----- |
| `image.registry` | Container image registry | `ghcr.io` |
| `image.repository` | Container image repository | `mateus-gsilva/kube-resource-updater` |
| `image.tag` | Image tag (immutable tags recommended). Empty pins the image to the chart appVersion. | `""` |
| `image.digest` | Image digest (e.g. sha256:abc...) for an immutable pin; overrides image.tag when set | `""` |
| `image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `image.pullSecrets` | Image pull secret names as an array | `[]` |

### CronJob parameters

| Name | Description | Value |
| ---- | ----------- | ----- |
| `cronjob.enabled` | Deploy the sync CronJob and its RBAC. Set to false on clusters that only need the admission webhook (the webhook is the per-cluster runtime; the CronJob runs in a single "control" cluster that has access to ArgoCD Applications and the GitLab token). | `true` |
| `cronjob.schedule` | Cron schedule expression | `0 */6 * * *` |
| `cronjob.timeZone` | Timezone (requires k8s >= 1.27) | `UTC` |
| `cronjob.concurrencyPolicy` | Forbid prevents overlapping runs | `Forbid` |
| `cronjob.successfulJobsHistoryLimit` | Number of successful jobs to retain | `3` |
| `cronjob.failedJobsHistoryLimit` | Number of failed jobs to retain | `1` |
| `cronjob.startingDeadlineSeconds` | Deadline in seconds for starting the job if it misses scheduled time | `""` |
| `cronjob.activeDeadlineSeconds` | Maximum duration in seconds the job may be active | `1800` |
| `cronjob.ttlSecondsAfterFinished` | Clean up finished Jobs after this many seconds | `3600` |
| `cronjob.backoffLimit` | Number of retries before marking the job failed | `2` |
| `cronjob.restartPolicy` | Pod restart policy | `OnFailure` |
| `command` | Override default container command | `[]` |
| `args` | Override default container args | `["sync"]` |

### Application config

| Name | Description | Value |
| ---- | ----------- | ----- |
| `config.gitProvider` | Git provider override: "" (auto-detect from repoUrl) \| "auto" \| "gitlab" \| "github" When empty, the tool infers the provider from the repoUrl host (github.com / github.* → GitHub; everything else → GitLab). Override here to force a specific provider or to wire GitHub Enterprise Server alongside a custom gitApiUrl. | `""` |
| `config.dryRun` | Skip git/MR operations (log only) | `false` |
| `config.createMr` | Open a GitLab MR (default) — false = direct push to crWriteback.branch | `true` |
| `config.gitAuthorName` | Git commit author name | `kube-resource-updater` |
| `config.gitAuthorEmail` | Git commit author email | `kube-resource-updater@cluster.local` |
| `config.cpuLimitMultiplier` | CPU limit = target × N (fallback when Prometheus unavailable) | `4` |
| `config.memoryLimitMultiplier` | Memory limit = target × N (fallback when Prometheus unavailable) | `3` |
| `config.minCpuLimitM` | Minimum CPU limit in millicores | `0` |
| `config.minMemoryLimitMi` | Minimum memory limit in MiB (0 = disabled) | `0` |
| `config.logLevel` | Log level: DEBUG, INFO, WARNING, ERROR | `INFO` |
| `config.logFormat` | Log format: text or json | `text` |
| `config.logColor` | ANSI-color output for text logs: auto (color when text format), always, never. Default "auto" emits muted 256-color escapes tuned for white-bg log viewers (Argo CD, GitLab Job logs, xterm-light). Set "never" to drop the escape sequences entirely (e.g. when piping through a non-ANSI-aware aggregator). JSON format already strips ANSI regardless. | `auto` |
| `config.cpuPercentile` | Percentile for CPU request estimation (prometheus source) | `0.90` |
| `config.memPercentile` | Percentile for memory request estimation (prometheus source) | `0.90` |
| `config.cpuRequestWindow` | Lookback window for CPU request percentile | `3d` |
| `config.memRequestWindow` | Lookback window for memory request percentile | `8d` |
| `config.cpuLimitWindow` | Lookback window for CPU limit (max_over_time) | `7d` |
| `config.memLimitWindow` | Lookback window for memory limit (max_over_time) | `7d` |
| `config.marginFraction` | Global fallback margin when per-type margins are not set (0 = no automatic margin) | `0.10` |
| `config.minCpuRequestM` | Minimum CPU request in millicores (0 = disabled) | `0` |
| `config.coldStartCpuFloorM` | Cold-start CPU floor in millicores applied when Prometheus has no history for the container | `10` |
| `config.minMemoryRequestMi` | Minimum memory request in MiB (0 = disabled) | `0` |
| `config.maxCpuRequestM` | Maximum CPU request in millicores (0 = disabled) | `0` |
| `config.maxMemoryRequestMi` | Maximum memory request in MiB (0 = disabled) | `0` |
| `config.maxCpuLimitM` | Maximum CPU limit in millicores (0 = disabled) | `0` |
| `config.maxMemoryLimitMi` | Maximum memory limit in MiB (0 = disabled) | `0` |
| `config.roundValues` | Round all computed resource values up to nearest order-of-magnitude step (101→200, 11→20) | `false` |
| `config.growOnly` | Global grow-only mode: only increase resource values, never decrease. Per-app annotation overrides. | `false` |
| `config.shrinkOnly` | Global shrink-only mode: only decrease resource values, never increase. Per-app annotation overrides. | `false` |
| `config.oomDetectionEnabled` | Detect OOMKilled and bump memory at sync time | `true` |
| `config.oomBumpFactor` | Multiplier on the limit at OOM time (1.5 = +50% headroom). Bumped value also stamped as the CR's `oom-floor` annotation. | `1.5` |
| `config.oomFloorEnabled` | Make OOM bumps sticky via `oom-floor.<container>` annotation | `true` |
| `config.healthGateEnabled` | Hold the recommendation when the sample window is untrustworthy | `true` |
| `config.maxRestartsInWindow` | Restarts tolerated inside the widest request window before holding (0 = gate off) | `3` |
| `config.minSampleCoverage` | Fraction of the window's evaluation points that must carry data before a percentile is trusted (0 = gate off) | `0.25` |
| `config.skipContainers` | Comma-separated container names to skip cluster-wide | `""` |
| `config.crWriteback.repoUrl` | Git repo URL where ResourceOverride CRs are committed (REQUIRED) | `""` |
| `config.crWriteback.branch` | Branch within crWriteback.repoUrl (defaults to "main") | `main` |
| `config.crWriteback.path` | In-repo directory where files land (REQUIRED) | `""` |
| `config.prometheusUrl` | Prometheus URL (REQUIRED when cronjob.enabled) | `""` |
| `config.gitlabUsername` | DEPRECATED — use git.username instead. Kept as a config-file alias so existing ConfigMaps that carry `gitlabUsername:` continue to work without operator action. The Python side reads git.username first, falls back to gitlabUsername. | `""` |
| `config.mr.assignees` | Comma-separated GitLab usernames to assign on MR open | `""` |
| `config.mr.reviewers` | Comma-separated GitLab usernames to add as reviewers | `""` |
| `config.mr.labels` | Comma-separated MR labels | `""` |
| `config.mr.squash` | Set MR's `Squash commits when merging` flag | `false` |
| `config.mr.removeSourceBranch` | Delete source branch after merge (kept true for the historical default) | `true` |

### Git credentials

| Name | Description | Value |
| ---- | ----------- | ----- |
| `git.token` | Canonical git token (provider-agnostic). Ignored when git.existingSecret is set. | `""` |
| `git.existingSecret` | Name of an existing Secret containing the git token. | `""` |
| `git.existingSecretKey` | Key within git.existingSecret that holds the token. | `token` |
| `git.apiUrl` | Optional API base URL override for GitHub Enterprise Server or self-hosted GitLab. When empty, the provider factory derives the URL from the repoUrl host. Examples: "https://ghe.mycompany.com/api/v3" (GitHub Enterprise), "https://gitlab.mycompany.com" (self-hosted GitLab). | `""` |
| `git.username` | Git HTTP username for token-based auth. Only needed when the token is a user-generated PAT that requires a username prefix (GitLab user PATs). Pipeline/project tokens and GitHub tokens do NOT need a username — they authenticate on the token alone. | `oauth2` |

### GitLab credentials (DEPRECATED — use git: above for new installations)

| Name | Description | Value |
| ---- | ----------- | ----- |
| `gitlab.token` | DEPRECATED — use git.token. GitLab personal/project token (ignored if existingSecret is set). | `""` |
| `gitlab.username` | DEPRECATED — use git.username. GitLab username for user-generated tokens. | `""` |
| `gitlab.existingSecret` | DEPRECATED — use git.existingSecret. Name of an existing Secret containing the token. | `""` |
| `gitlab.existingSecretKey` | DEPRECATED — use git.existingSecretKey. Key within the existing Secret. | `token` |

### ServiceAccount

| Name | Description | Value |
| ---- | ----------- | ----- |
| `serviceAccount.create` | Create the CronJob ServiceAccount | `true` |
| `serviceAccount.name` | Override the generated ServiceAccount name | `""` |
| `serviceAccount.annotations` | Extra annotations for the ServiceAccount | `{}` |
| `serviceAccount.automountServiceAccountToken` | Mount the SA token into the pod (required: the sync calls the Kubernetes API) | `true` |

### RBAC

| Name | Description | Value |
| ---- | ----------- | ----- |
| `rbac.create` | Create the ClusterRole/Role + bindings the tool needs | `true` |

### Pod Disruption Budget

| Name | Description | Value |
| ---- | ----------- | ----- |
| `pdb.create` | Create a PodDisruptionBudget | `false` |
| `pdb.minAvailable` | Minimum number of pods that must be available (mutually exclusive with maxUnavailable) | `""` |
| `pdb.maxUnavailable` | Maximum number of pods that can be unavailable (mutually exclusive with minAvailable; empty falls back to 1 unless minAvailable is set) | `""` |

### Network Policy

| Name | Description | Value |
| ---- | ----------- | ----- |
| `networkPolicy.enabled` | Enable NetworkPolicy | `false` |
| `networkPolicy.allowExternal` | Allow connections from pods without the matching label | `true` |
| `networkPolicy.allowExternalEgress` | Allow all egress traffic (required: git, k8s API, Prometheus) | `true` |
| `networkPolicy.extraIngress` | Extra ingress rules to add | `[]` |
| `networkPolicy.extraEgress` | Extra egress rules to add | `[]` |

### Pod scheduling

| Name | Description | Value |
| ---- | ----------- | ----- |
| `podAffinityPreset` | Pod affinity preset: "" (disabled), soft, hard | `""` |
| `podAntiAffinityPreset` | Pod anti-affinity preset: "" (disabled), soft, hard | `""` |
| `nodeAffinityPreset` | Node affinity preset | `{"type":"","key":"","values":[]}` |
| `nodeAffinityPreset.type` | Node affinity type: "" (disabled), soft, hard | `""` |
| `nodeAffinityPreset.key` | Node label key to match | `""` |
| `nodeAffinityPreset.values` | Node label values to match | `[]` |
| `affinity` | Raw affinity rules (overrides presets when set) | `{}` |
| `nodeSelector` | Node label selector for pod assignment | `{}` |
| `tolerations` | Tolerations for pod assignment | `[]` |
| `topologySpreadConstraints` | Topology spread constraints | `[]` |
| `hostAliases` | Custom /etc/hosts entries for pods | `[]` |
| `priorityClassName` | Priority class name | `""` |
| `schedulerName` | Custom scheduler name | `""` |
| `terminationGracePeriodSeconds` | Grace period in seconds before SIGKILL | `30` |

### Pod metadata

| Name | Description | Value |
| ---- | ----------- | ----- |
| `podAnnotations` | Extra annotations for pods | `{}` |
| `podLabels` | Extra labels for pods | `{}` |

### Security contexts

| Name | Description | Value |
| ---- | ----------- | ----- |
| `podSecurityContext.enabled` | Enable the pod-level security context | `true` |
| `podSecurityContext.fsGroup` | Group ID for the volumes mounted into the pod | `1001` |
| `podSecurityContext.runAsUser` | User ID for all containers in the pod | `1001` |
| `podSecurityContext.runAsGroup` | Primary group ID for all containers in the pod | `1001` |
| `podSecurityContext.runAsNonRoot` | Require the pod's containers to run as a non-root user | `true` |
| `podSecurityContext.seccompProfile.type` | Seccomp profile for all containers (RuntimeDefault required by PSA `restricted`) | `RuntimeDefault` |
| `containerSecurityContext.enabled` | Enable the container-level security context | `true` |
| `containerSecurityContext.runAsNonRoot` | Require the container to run as a non-root user | `true` |
| `containerSecurityContext.runAsUser` | User ID for the container | `1001` |
| `containerSecurityContext.allowPrivilegeEscalation` | Allow privilege escalation in the container | `false` |
| `containerSecurityContext.readOnlyRootFilesystem` | Mount the container root filesystem as read-only | `true` |
| `containerSecurityContext.capabilities.drop` | List of Linux capabilities to drop | `["ALL"]` |
| `resources` | Container resource requests and limits | `{"requests":{"cpu":"100m","memory":"128Mi"},"limits":{"cpu":"500m","memory":"256Mi"}}` |

### Mutation Webhook

| Name | Description | Value |
| ---- | ----------- | ----- |
| `webhook.enabled` | Enable the mutation webhook Deployment, Service, MutatingWebhookConfiguration, and (when validating.enabled) the ValidatingWebhookConfiguration. The webhook owns its own serving cert through an in-process reconciler — no cert-manager dependency. | `false` |
| `webhook.replicaCount` | Number of webhook replicas (1 = single-runner; ≥ 2 for HA, see trade-offs above) | `1` |
| `webhook.revisionHistoryLimit` | Number of old ReplicaSets to retain for rollback. 0 keeps only the current one. Default 10 (Kubernetes default). Lower values shrink the ArgoCD UI tree for clusters that get frequent webhook image bumps; 0 means no rollback history (`kubectl rollout undo` won't work, but live deploy is unaffected). | `10` |
| `webhook.deploymentAnnotations` | Annotations on the webhook Deployment's own metadata (NOT the pod template — use podAnnotations for that). For controllers that read Deployment-level annotations, e.g. Stakater Reloader: `configmap.reloader.stakater.com/reload: <configmap-name>`. | `{}` |
| `webhook.port` | TLS port the webhook listens on for AdmissionReview | `9443` |
| `webhook.metricsPort` | Plain-HTTP port for /healthz, /readyz, /metrics | `8080` |
| `webhook.failurePolicy` | Behaviour when webhook is unreachable. `Ignore` lets pods admit with chart defaults; `Fail` blocks the pod. | `Ignore` |
| `webhook.timeoutSeconds` | Per-request admission timeout | `5` |
| `webhook.sideEffects` | Standard k8s admission field; `None` documents that the webhook does not affect external state | `None` |
| `webhook.resources.requests` | CPU/memory requests for the webhook pod | `{"cpu":"50m","memory":"96Mi"}` |
| `webhook.resources.limits` | CPU/memory limits for the webhook pod | `{"cpu":"200m","memory":"256Mi"}` |
| `webhook.caBundle` | Optional base64-encoded CA bundle for the webhook TLS cert (default empty → in-process reconciler owns it) | `""` |
| `webhook.validating.enabled` | Reject CR creates/updates whose selector + container set overlaps an existing CR in the same namespace. | `true` |
| `webhook.autoRollout.enabled` | Cluster-wide default for the auto-rollout hierarchy. Per-workload / per-namespace annotations override this. | `false` |
| `webhook.autoRollout.debounceSeconds` | How long to wait after the last CR change before patching the workload's PodTemplate. | `30` |
| `webhook.status.enabled` | Write CR.status.appliedToPodCount + lastAppliedAt; controls the RBAC verb on resourceoverrides/status too. | `false` |
| `webhook.status.flushIntervalSeconds` | Coalesce window for the status writer (seconds). | `30` |
| `webhook.metrics.serviceMonitor.enabled` | Create a ServiceMonitor for /metrics | `false` |
| `webhook.metrics.serviceMonitor.namespace` | Namespace for the ServiceMonitor (defaults to release namespace) | `""` |
| `webhook.metrics.serviceMonitor.interval` | Scrape interval | `30s` |
| `webhook.metrics.serviceMonitor.labels` | Extra labels. Must match your Prometheus operator's serviceMonitorSelector — e.g. `release: kube-prometheus-stack` (the Helm release name of your kube-prometheus-stack install). Empty by default: with a strict selector the ServiceMonitor is silently NOT scraped until this is set. | `{}` |
| `webhook.metrics.prometheusRule.enabled` | Create a PrometheusRule with webhook alerting rules (requires prometheus-operator). Pairs with serviceMonitor.enabled — alerts on the webhook failing open (failurePolicy: Ignore) so a down webhook pages instead of silently admitting pods with chart-default resources. | `false` |
| `webhook.metrics.prometheusRule.namespace` | Namespace for the PrometheusRule (defaults to release namespace) | `""` |
| `webhook.metrics.prometheusRule.labels` | Extra labels for the PrometheusRule. Must match your Prometheus operator's ruleSelector — e.g. `release: kube-prometheus-stack`. Empty by default: with a strict selector the rules are silently NOT loaded until this is set. | `{}` |
| `webhook.podDisruptionBudget.enabled` | Create a PDB for the webhook Deployment. Default false because the chart's `webhook.replicaCount: 1` default plus a PDB with `minAvailable: 1` would block every node drain involving the webhook pod (single replica can't satisfy the budget). Operators who bump replicaCount to >= 2 should flip this to true. Chart's validate.yaml fails install when PDB is on with replicaCount=1. | `false` |
| `webhook.podDisruptionBudget.minAvailable` | Minimum number of available webhook replicas | `1` |
| `webhook.nodeSelector` | Node selector for webhook pods | `{}` |
| `webhook.tolerations` | Tolerations for webhook pods | `[]` |
| `webhook.affinity` | Affinity rules for webhook pods (empty = the chart applies soft anti-affinity by hostname) | `{}` |

### Extra configuration

| Name | Description | Value |
| ---- | ----------- | ----- |
| `extraEnvVars` | Extra environment variables | `[]` |
| `extraEnvVarsCM` | Name of ConfigMap with extra environment variables | `""` |
| `extraEnvVarsSecret` | Name of Secret with extra environment variables | `""` |
| `extraVolumes` | Extra volumes | `[]` |
| `extraVolumeMounts` | Extra volume mounts | `[]` |
| `initContainers` | Extra init containers | `[]` |
| `sidecars` | Extra sidecar containers | `[]` |

## Validating an install

```console
# 1. Check the CronJob has the right env from the ConfigMap
kubectl -n kube-resource-updater describe cronjob kube-resource-updater | grep -A30 Environment

# 2. Trigger a one-off sync without waiting for the schedule
kubectl -n kube-resource-updater create job --from=cronjob/kube-resource-updater kube-resource-updater-manual
kubectl -n kube-resource-updater logs -l job-name=kube-resource-updater-manual -f

# 3. Confirm the webhook is admitting (look at one new pod in an opted-in namespace)
kubectl get pod <pod> -o jsonpath='{.spec.containers[0].resources}'
```

## Verifying the chart signature

Chart releases are signed with [cosign](https://docs.sigstore.dev/) (keyless, via GitHub Actions OIDC). Verify a pulled chart with:

```console
cosign verify ghcr.io/mateus-gsilva/charts/kube-resource-updater:<version> \
  --certificate-identity-regexp 'https://github.com/mateus-gsilva/kube-resource-updater/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Configuration and installation details

See [`values.yaml`](values.yaml) for the full annotated list of knobs, and:

- Tool reference, annotations, OOM-aware bump algorithm, RBAC: [`docs/reference.md`](https://github.com/mateus-gsilva/kube-resource-updater/blob/main/docs/reference.md)
- Architecture rationale + design: [`docs/webhook-migration.md`](https://github.com/mateus-gsilva/kube-resource-updater/blob/main/docs/webhook-migration.md)
- Roadmap + release history: [`ROADMAP.md`](https://github.com/mateus-gsilva/kube-resource-updater/blob/main/ROADMAP.md), [`CHANGELOG.md`](https://github.com/mateus-gsilva/kube-resource-updater/blob/main/CHANGELOG.md)

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](https://github.com/mateus-gsilva/kube-resource-updater/blob/main/LICENSE).
