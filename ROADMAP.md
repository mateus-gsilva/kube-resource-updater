# kube-resource-updater — Roadmap

Pending work and known gaps. Release history in [CHANGELOG.md](CHANGELOG.md).
Architecture: [docs/webhook-migration.md](docs/webhook-migration.md). Operator
reference: [docs/reference.md](docs/reference.md).

> **Complexity:** 🟢 low · 🟡 medium · 🔴 high

---

## Confirmed bugs

| | Item |
|---|---|
| 🟡 | **Crash-loop gate is inert without kube-state-metrics.** The gate added for the 2026-07-20 undersizing incident reads `kube_pod_container_status_restarts_total`. On a cluster without kube-state-metrics the query returns no data, the gate fails open, and a workload can still be sized from a crash-looping pod's samples — the tool warns once per sync but cannot stop it. The coverage gate still applies (it uses cAdvisor series the tool already requires) and catches the short-history subset of the same failure, so this is a partial gap, not a full regression. Closing it means a second, kubelet-only restart signal. |
| 🟡 | **Grow/shrink has no baseline when both OOM detection and the health gate are disabled.** `_apply_grow_shrink` compares against the live CR's container resources, which only reach the build phase via `fetch_oom_state`. `cmd_sync` calls that when either `oomDetectionEnabled` or `healthGateEnabled` resolves true — so with both off, `growOnly` / `shrinkOnly` silently no-op instead of clamping. The CR-state fetch should not be conditional on either feature. |

## Known gaps (0.x series)

| | Item |
|---|---|
| 🟢 | **GitHub assignees.** The `GitHubProvider` accepts `mr.assignees` but does not yet apply them (needs a PATCH to the Issues API); reviewer requests work. GitLab applies assignees, reviewers, and labels. TODO in `src/git_provider.py`. |
| 🔴 | **Integration test suite.** The offline QA suite (~1,250 asserts) is comprehensive at the unit/render layer, but cluster-based integration tests (real apiserver + webhook + kubelet sequencing, failure injection, scale, upgrade path) are not yet automated. |

## Planned features

### Recommendation quality

| | Item |
|---|---|
| 🔴 | **CPU-throttle-aware bump (mirrors OOM-aware)** — driven by `container_cpu_cfs_throttled_seconds_total / container_cpu_cfs_periods_total`. Bump path `new_limit = current_limit × cpuBumpFactor` (default 1.25), per-container sticky floors and history annotations mirroring the OOM design. Open design questions: continuous-signal trap semantics (OOM is binary, throttling isn't), false positives on bursty batch workloads, latency expectations, interaction with the multiplier-based limit. |

### Observability

| | Item |
|---|---|
| 🟡 | **Prometheus metrics endpoint for the sync** — push per-run counters (workloads updated/skipped/error per namespace, last-sync timestamp) to a Pushgateway. |
| 🔴 | **Grafana dashboard** — provisionable JSON backed by the metrics endpoint: recommendations over time, skipped workloads, CPU/mem delta distribution, OOM boosts applied. |
| 🟡 | **MR description: HPA hint** — when a request changes on a workload with an HPA, note how the target % shifts. |
| 🟡 | **`kru diff <workload>` inspection command** — single-workload diff showing Prometheus values + margins + OOM history, answering "why this recommendation?" without manual queries. |

### Write-back modes

| | Item |
|---|---|
| 🟡 | **push-only / direct modes** — `config.writebackMode: gitops \| push-only \| direct`. push-only commits + pushes the branch and stops (user opens the PR); direct applies CRs straight to the cluster API, trading the git audit trail for latency. Default stays `gitops`. |
| 🟡 | **Validate GitHub App auth against a live GitHub App** — App auth shipped in 0.1.4 but is **alpha**: the JWT → installation-token exchange and the "App not installed" 401/403 path are mock-tested only. Exercise against a real GitHub App install (token mint, MR open) before recommending it for production. |

### Workload coverage

| | Item |
|---|---|
| 🟡 | **Cluster-wide default-on (opt-out) mode** — `config.defaultEnabled: true` treats every namespace as enabled except those in `config.excludeNamespaces` or carrying `kube-resource-updater.enabled: "false"` (explicit per-namespace opt-out); `kube-system` and the release namespace stay force-excluded. Lets an org cover a cluster without annotating each namespace one by one. Pairs with the exclude list to keep critical control-plane (vault, argocd, monitoring, etc.) out. Effort: M (discovery predicate + config field + chart values + QA). |
| 🟡 | **Node pool awareness** — cap recommendations at the largest node available in the workload's node pool. |
| 🟡 | **Native sidecar containers (k8s 1.28+)** — the webhook only patches `/spec/containers/*`; long-running sidecars declared in `spec.initContainers` (`restartPolicy: Always`) are skipped. |

### Cost & analysis

| | Item |
|---|---|
| 🟡 | **Cost delta in MR description** — estimated monthly cost change per workload from node pricing (OpenCost or configurable price-per-core/GiB). |

### Other

- **Startup profiling via Metrics Server** — initial sizing for workloads with no Prometheus history; poll metrics-server during the startup window, capture peaks, write initial requests/limits.
- **GitOps drift: SSA reconciler for workload templates** — overrides only appear at admission, so `kubectl get deploy -o yaml` differs from running pods. Design in [docs/webhook-migration.md](docs/webhook-migration.md).
