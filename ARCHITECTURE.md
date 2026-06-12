# Architecture

The current architecture is documented in purpose-built docs rather than
restated here, so there's a single source of truth per topic:

- **Design + rationale** (webhook + `ResourceOverride` CRD): [`docs/webhook-migration.md`](docs/webhook-migration.md)
- **User-facing reference** (annotations, config, RBAC, OOM-aware, JSON logs): [`docs/reference.md`](docs/reference.md)
- **Quick summary + install**: [`README.md`](README.md)
- **Release history**: [`CHANGELOG.md`](CHANGELOG.md) · **pending work**: [`ROADMAP.md`](ROADMAP.md)

## One-paragraph overview

A CronJob reads workload usage from Prometheus, computes right-sized
requests/limits, and writes one `ResourceOverride` custom resource per workload
to a Git repository (one Merge/Pull Request per repo per run). A GitOps engine
(Argo CD, Flux, or plain `kubectl`) syncs those CRs into the cluster. An
in-cluster **mutating admission webhook** then patches pod resources at
admission time from the matching CR — workloads are matched by label selector +
container name, never by Helm value-tree position. The webhook runs
`failurePolicy: Ignore`, so an outage fails open (pods admit unpatched) rather
than blocking admission cluster-wide.
