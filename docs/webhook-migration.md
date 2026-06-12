# Architecture: mutation webhook + ResourceOverride CRD

This is the design record for the tool's architecture — the rationale, CRD
schema, RBAC, and trade-offs below all reflect the running code.

**Why this design:** the obvious way to right-size workloads is to write the
`resources:` block into each chart's Helm `values:` tree. That hits a hard
ceiling on multi-source apps, `$values` reference patterns, charts that don't
expose sub-component resources, workload names that don't tokenize into chart
paths, and shared paths between two workloads — all unsolvable without per-chart
special cases. Instead, the tool ships resource overrides as a uniform Custom
Resource (`ResourceOverride`) and applies them via mutating admission,
independent of the underlying app's chart structure.

---

## Goal

Stop writing resources into Helm value trees. Move resource overrides to a single, uniform
representation that is independent of the chart structure: a Custom Resource (`ResourceOverride`)
applied by a mutating admission webhook at Pod creation time.

**Net effect for the operator:** every workload in every cluster has its resources defined in
exactly one place, in exactly one format, regardless of whether the underlying app is Helm,
Kustomize, multi-source, or a private chart.

**Net effect for the codebase:** the Helm path detection module (two-pass scoring, sole-survivor
rule, registry of chart-specific paths, `helm show values` calls, `resources: {}` placeholder
discipline) can be deleted in its entirety.

---

## Architecture

### Components

1. **`ResourceOverride` CRD** (cluster-scoped CRD, namespace-scoped resource)
   - Defines the desired resources for a set of containers in a set of pods.
   - Selector-based — matches pods by labels, container by name.

2. **Mutation webhook** (Go, controller-runtime)
   - Watches `ResourceOverride` CRs via informer cache.
   - On Pod `CREATE` / `UPDATE` admission, finds matching `ResourceOverride`s and patches
     `pod.spec.containers[].resources` and `pod.spec.initContainers[].resources`.
   - `failurePolicy: Ignore` — if the webhook is down, pods admit with chart defaults.

3. **`kube-resource-updater` (existing tool, refactored writeback)**
   - Reads recommendations from Prometheus (unchanged).
   - Instead of writing into `spec.sources[].helm.values`, writes a `ResourceOverride` YAML
     file per workload into a dedicated GitOps folder.
   - Opens a single MR per repo (unchanged).

### Data flow

```
Prometheus
   └── container_cpu_usage_seconds_total
       container_memory_working_set_bytes
            │ p80 / 7d max
            ▼
kube-resource-updater sync
            │
            ▼
   gitops repo: clusters/<cluster>/resource-overrides/<ns>/<workload>.yaml
            │
            ▼
   GitLab MR ──► reviewer approves ──► merge
            │
            ▼
   ArgoCD Application "resource-overrides-<cluster>" syncs the folder
            │
            ▼
   ResourceOverride CR present in cluster
            │
            ▼  (next pod admission)
   Webhook patches pod.spec.containers[].resources
            │
            ▼
   Pod scheduled with effective resources
```

---

## ResourceOverride CRD

```yaml
apiVersion: kube-resource-updater.io/v1
kind: ResourceOverride
metadata:
  name: redis-master
  namespace: redis
spec:
  selector:
    matchLabels:
      app.kubernetes.io/instance: redis
      app.kubernetes.io/component: master
  containers:
    - name: redis
      requests: { cpu: "100m", memory: "256Mi" }
      limits:   { cpu: "500m", memory: "512Mi" }
    - name: metrics
      requests: { cpu: "10m",  memory: "32Mi"  }
      limits:   { cpu: "50m",  memory: "64Mi"  }
status:
  appliedToPodCount: 3
  lastAppliedAt: "2026-04-28T14:22:11Z"
```

### Field semantics

- `spec.selector` — standard `LabelSelector`. Matches pods within the same namespace as the CR.
- `spec.containers[].name` — must equal `pod.spec.containers[].name` (or `initContainers[].name`).
  Containers not listed are not patched.
- `spec.containers[].requests` / `limits` — `ResourceList` (same shape as core/v1).

### Validation rules (admission via OpenAPI v3 schema + validating webhook)

- Selector must match at least one pod when CR is created (warn, do not block).
- Container name must be present in at least one pod matched by selector (warn).
- `limits` ≥ `requests` per resource type.
- No two `ResourceOverride`s in the same namespace may match overlapping (pod, container) pairs
  — validating webhook rejects creation if conflict detected.

---

## Mutation webhook

### Behaviour

- **Triggers:** `Pod` resource, operations `CREATE` and `UPDATE`.
- **Selection:** for each pod, list `ResourceOverride`s in the same namespace (informer cache,
  no API call). For each CR whose `spec.selector` matches the pod's labels, apply patches.
- **Patch:** JSON Patch on `pod.spec.containers[i].resources` and `initContainers[i].resources`.
  Existing `requests`/`limits` keys in the pod spec are **overwritten** for the listed containers.
  Containers not listed in any matching CR are left untouched.
- **Annotation:** webhook adds `kube-resource-updater.applied-from: <cr-namespace>/<cr-name>`
  to the pod for traceability (visible via `kubectl describe pod`).

### Configuration

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: resource-updater-webhook
  annotations:
    cert-manager.io/inject-ca-from: resource-updater-system/resource-updater-webhook-cert
webhooks:
  - name: pods.kube-resource-updater.io
    clientConfig:
      service:
        name: resource-updater-webhook
        namespace: resource-updater-system
        path: /mutate-pod
    rules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        resources: ["pods"]
        operations: ["CREATE", "UPDATE"]
        scope: "Namespaced"
    failurePolicy: Ignore
    sideEffects: None
    admissionReviewVersions: ["v1"]
    namespaceSelector:
      matchExpressions:
        # Opt-in: namespaces participate only if labelled with the project's enabled key.
        - key: kube-resource-updater.enabled
          operator: In
          values: ["true"]
        # Defence in depth: never touch system namespaces.
        - key: kubernetes.io/metadata.name
          operator: NotIn
          values: ["kube-system", "resource-updater-system"]
    objectSelector:
      matchExpressions:
        - key: kube-resource-updater.skip
          operator: DoesNotExist
    timeoutSeconds: 5
```

Key choices:
- **`failurePolicy: Ignore`** — webhook outage cannot block deployments. Pods admit with helm
  defaults; the tool's metrics endpoint surfaces the gap.
- **Namespace-label opt-in** — only namespaces explicitly labelled
  `kube-resource-updater.enabled: "true"` are intercepted. Namespaces without the label
  never reach the webhook, so unrelated Helm releases pay zero admission overhead and have
  zero risk of being modified. This follows the standard pattern used by Istio, Linkerd, and
  OPA Gatekeeper for cluster-wide admission infrastructure.
- **`objectSelector` opt-out** — pods labelled `kube-resource-updater.skip: "true"` bypass
  the webhook entirely (per-pod debug escape hatch, complements the namespace-level opt-in).
- **TLS via cert-manager** — `Certificate` resource generates a serving cert; `caBundle` is
  injected via cert-manager's `inject-ca-from` annotation.

### Opt-in flow per Application

For an ArgoCD Application to participate in webhook-based resource management, two things
must be true:

1. **The namespace it deploys into must carry the opt-in label.** This is wired via the
   Application's `syncPolicy.managedNamespaceMetadata`:
   ```yaml
   spec:
     syncPolicy:
       managedNamespaceMetadata:
         labels:
           kube-resource-updater.enabled: "true"
   ```
   This makes opt-in fully GitOps-visible — the Application YAML diff shows whether a release
   participates in resource management. No magic, no implicit behaviour, no out-of-band tool
   labelling.

2. **The application must have at least one `ResourceOverride` CR generated by the sync tool.**
   The tool already filters apps via `kube-resource-updater.enabled: "true"` annotation; that
   stays unchanged. The annotation gates *generation* of CRs; the namespace label gates
   *enforcement* by the webhook. Both must be set for the workload to be managed end-to-end.

Apps that don't set the namespace label (or are deployed into namespaces without it) keep
their chart-rendered resources verbatim. The webhook never sees their pods.

### Performance budget

- Informer cache eliminates per-admission API calls.
- Selector matching is in-memory (label hashing).
- p99 admission latency target: < 5 ms.
- Memory footprint: ~50 MiB for ~1000 CRs in cache.

---

## Git layout

### Per-cluster folder

Existing structure (unchanged):
```
gitops/clusters/<cluster>/applications/
  ├── redis.yaml          # ArgoCD Application (helm)
  ├── kube-prometheus-stack.yaml
  └── ...
```

New parallel folder:
```
gitops/clusters/<cluster>/resource-overrides/
  ├── monitoring/
  │   ├── prometheus.yaml          # ResourceOverride CR
  │   ├── alertmanager.yaml
  │   └── kube-state-metrics.yaml
  ├── redis/
  │   ├── redis-master.yaml
  │   └── redis-replicas.yaml
  └── kyverno/
      ├── kyverno.yaml
      └── policy-reporter.yaml
```

One file per workload, one folder per namespace. Filename = `<workload>.yaml` where
`<workload>` corresponds to the deployment / statefulset name.

### New ArgoCD Application

```yaml
# gitops/clusters/<cluster>/applications/resource-overrides.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: resource-overrides-<cluster>
  namespace: argocd
  annotations:
    kube-resource-updater.enabled: "false"   # the tool does not modify this app itself
spec:
  project: <cluster-project>
  source:
    repoURL: https://<your-gitlab-host>/infra/gitops.git
    path: clusters/<cluster>/resource-overrides
    directory:
      recurse: true
    targetRevision: HEAD
  destination:
    name: <cluster>
    namespace: ""    # CRs are namespaced; namespace comes from each CR file
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=false   # namespaces must already exist (created by their respective apps)
```

### MR shape

Today: tool clones N repos (one per Application's `gitops-repo`), edits `spec.sources[].helm.values`
in 30+ Application YAMLs, opens up to N MRs (one per repo).

Tomorrow: tool clones one repo per cluster gitops, edits/creates files under
`resource-overrides/`, opens one MR per cluster gitops repo.

Diff is uniform — every changed file looks like a `ResourceOverride`. No more navigating
chart-specific values structures during review.

### Drift handling

ArgoCD will see the running pod's `containers[].resources` as different from what the helm
chart renders (because the webhook injects values that aren't in the chart's output). To
prevent permanent `OutOfSync` status, configure `ignoreDifferences` globally:

```yaml
# In each AppProject (or ApplicationSet template)
spec:
  ignoreDifferences:
    - group: ""
      kind: Pod
      jsonPointers:
        - /spec/containers/*/resources
        - /spec/initContainers/*/resources
    - group: apps
      kind: Deployment
      managedFieldsManagers:
        - resource-updater-webhook
    - group: apps
      kind: StatefulSet
      managedFieldsManagers:
        - resource-updater-webhook
```

The webhook patches Pods, but the recommended pattern is to also have the controller mutate
the owning Deployment/StatefulSet's pod template via SSA (server-side apply) so that helm
upgrades don't reset resources. This is a future enhancement.

---

## Tool refactor

### What changes

- **`writeback.py`** — the entire `_update_app_inline_values` flow is replaced by
  `_write_resource_override_cr`. The new function does not need:
  - Helm path detection (`_detect`, `_score_path`, `detect_helm_path` registry).
  - `helm show values` calls.
  - The `resources: {}` placeholder discipline.
  - `used_paths` deduplication.
  - Inline values YAML round-trip parsing.
- **`writeback_kustomize.py`** — the `.argocd-source-<app>.yaml` overlay path is also
  replaced by `ResourceOverride` CRs (kustomize apps go through the same flow).
- **`argocd.py`** — `writeback_source()` simplifies. The repo to clone is always the cluster's
  gitops repo (which already hosts the Application YAML). No more `gitops-repo` annotation,
  no more `app-repo` disambiguation.
- **`prometheus.py`** — unchanged. Resource computation logic is unchanged.
- **`config.py`** — adds `webhook_mode: bool` (default `false` during transition).

### Compatibility flag

```yaml
# helm values for the tool
config:
  writebackMode: webhook   # values: helm | webhook | both
```

- `helm` (default) — current behaviour, write into `spec.sources[].helm.values`.
- `webhook` — new behaviour, write `ResourceOverride` CRs.
- `both` — write to both during migration. Allows running the new pipeline without removing
  the old. Once verified, switch to `webhook` and clean up.

### Workload identity

A `ResourceOverride` is keyed by `(namespace, workload-name)`. The tool determines the workload
from Prometheus labels (`namespace`, `pod` → owner via `kube_pod_owner` or by stripping the
ReplicaSet hash from the pod name). The selector embedded in the CR is derived from
the workload's standard labels:
- `app.kubernetes.io/instance: <release-name>` (preferred when present)
- `app.kubernetes.io/component: <component>` (when present, e.g. `master` / `replicas`)
- fallback: exact match on the deployment / statefulset name via `app: <name>` if no
  standard labels are present

This is robust across Helm, Kustomize, raw YAML, and operator-managed workloads.

---

## What this fixes

| Existing bug / limitation | Resolution |
|---|---|
| BUG-01 — Multi-source helm apps: only first chart processed | Eliminated. `ResourceOverride` is unrelated to source structure. |
| BUG-02 — Shared helm path: two workloads compete | Eliminated. Two CRs with disjoint selectors patch independently. |
| BUG-03 — `$values` reference pattern | Eliminated. CR is independent of where chart values live. |
| BUG-04 — Bitnami `redis-node` name mismatch | Eliminated. Selector matches by labels, container by name. |
| BUG-05 — `argocd-image-updater` not in chart | Eliminated. CR works regardless of chart structure. |
| `resources: {}` placeholder requirement | Eliminated. |
| Two-pass detection + scoring + sole-survivor rule | Code deleted. |
| `detect_helm_path` chart registry | Code deleted. |
| `helm-values-path.<workload>` annotation | Deprecated, removed after migration. |
| `gitops-repo` annotation for pure chart-source apps | Deprecated, no longer needed. |
| `app-repo` annotation for multi-source disambiguation | Deprecated, no longer needed. |
| Duplicate YAML keys crashing parser | Eliminated. Tool no longer parses helm values. |

---

## Tradeoffs

1. **Application timing** — today, merging an MR triggers ArgoCD sync → helm renders →
   Deployment rolls out → new pods immediately have the new resources. Tomorrow, merging
   an MR only updates the CR; existing pods keep their current resources until they restart
   for any other reason. **Mitigation:** the tool can optionally trigger a `kubectl rollout
   restart` for affected workloads after merge (opt-in via flag, off by default).

2. **HPA interaction** — HPA scales by request value. If the webhook patches request differently
   from what helm rendered, the HPA's target percentage shifts. Same problem the current
   approach has — no regression.

3. **VPA coexistence** — if VPA is installed in a namespace and managing a workload, both will
   compete. **Mitigation:** the webhook checks for an existing VPA in the namespace targeting
   the same workload and skips patching. Logged as `[skip] vpa-managed: <namespace>/<workload>`.

4. **GitOps purity** — purists will note that the cluster state (pod resources) no longer
   matches what helm renders from the chart. This is by design — helm renders defaults, the
   webhook overrides at admission. The CR is the source of truth for resource overrides,
   committed to git, reviewed via MR. The architecture remains GitOps-compliant; only the
   layer where resources are declared changes.

5. **Webhook outage** — `failurePolicy: Ignore` means pods admit with helm defaults during
   an outage. Failure mode is "the tool isn't applying recommendations" — same as the current
   tool being down. No new failure mode introduced.

6. **CRD evolution** — versioning the CRD requires conversion webhooks if `v1` ever becomes
   `v2`. Stick to `v1` and use additive-only changes; add new fields rather than renaming
   existing ones.

7. **Pod-level vs workload-level patching** — the webhook patches pods (admission control on
   `Pod`). On helm upgrade, the new chart-rendered Deployment template will not contain the
   override (the override only appears at admission time). This means the running ReplicaSet's
   pod template *does* have the override (because it was patched), but the next helm-rendered
   Deployment template won't — leading to ArgoCD seeing drift. **Mitigation:** also
   reconcile Deployment/StatefulSet templates via SSA from a separate controller, so the
   template itself reflects the overrides. The webhook becomes a backstop for pods that
   bypass the controller.

---

## Open questions

- Should `ResourceOverride` be cluster-scoped instead of namespaced? Cluster-scoped lets one CR
  cover multiple namespaces (e.g. all `prometheus-*` namespaces). Namespaced is more
  conventional and easier to RBAC. **Decision: namespaced for v1.**
- Should the webhook also annotate the parent Deployment/StatefulSet (not just the pod) so that
  `kubectl get deploy -o yaml` shows the override? Adds a controller component. **Decision:
  a future enhancement, not in initial scope.**
- Status sub-resource — should `appliedToPodCount` be tracked? Useful for the Grafana dashboard
  (ROADMAP "Observability" section) but adds a controller. **Decision: deferred.**
- Do we want a CLI command on the tool to `kubectl rollout restart` after merge, or is that
  out of scope? **Decision: opt-in flag in tool, off by default.**
