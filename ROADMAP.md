# kube-resource-updater — Roadmap

Pending work and known gaps. Release history in [CHANGELOG.md](CHANGELOG.md).
Architecture: [docs/webhook-migration.md](docs/webhook-migration.md). Operator
reference: [docs/reference.md](docs/reference.md).

> **Complexity:** 🟢 low · 🟡 medium · 🔴 high

---

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

### Workload coverage

| | Item |
|---|---|
| 🟡 | **Node pool awareness** — cap recommendations at the largest node available in the workload's node pool. |
| 🟡 | **Native sidecar containers (k8s 1.28+)** — the webhook only patches `/spec/containers/*`; long-running sidecars declared in `spec.initContainers` (`restartPolicy: Always`) are skipped. |

### Cost & analysis

| | Item |
|---|---|
| 🟡 | **Cost delta in MR description** — estimated monthly cost change per workload from node pricing (OpenCost or configurable price-per-core/GiB). |

### Other

- **Startup profiling via Metrics Server** — initial sizing for workloads with no Prometheus history; poll metrics-server during the startup window, capture peaks, write initial requests/limits.
- **GitOps drift: SSA reconciler for workload templates** — overrides only appear at admission, so `kubectl get deploy -o yaml` differs from running pods. Design in [docs/webhook-migration.md](docs/webhook-migration.md).
