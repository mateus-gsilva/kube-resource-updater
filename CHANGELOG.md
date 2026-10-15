# Changelog

Release history for `kube-resource-updater`. Pending work lives in [ROADMAP.md](ROADMAP.md).

---

## 0.2.0 — Direct write-back and change guardrails (2026-10-15)

**Added — `config.writebackMode: direct`.** The sync can server-side apply the
rendered `ResourceOverride`s straight to the cluster API (field manager
`kube-resource-updater`) instead of committing them to git. No repository, token
or author identity is needed. Tool-owned CRs the sync no longer renders are
deleted — only CRs labelled `app.kubernetes.io/managed-by:
kube-resource-updater` (re-checked on each listed object) whose workload is
genuinely gone: not discovered in a namespace whose discovery, Prometheus queries
and live-CR read all succeeded, opted out with `skip: "true"`, or in a namespace
that stopped opting in. Every discovered workload is kept even when it produced
no CR this sync (held, no data, name too long); nothing is deleted when the sync
produced no CRs, the listing fails, or the workload is in `dryRun`. A namespace
whose live CRs cannot be read is neither applied nor pruned that sync. A CR whose
`spec` another field manager owns (still in an Argo CD Application,
hand-applied) is skipped with an error unless `config.direct.adoptForeignCRs:
true` is set for a deliberate migration.
The audit trail moves onto the CR: `kube-resource-updater.io/last-changed` and
`kube-resource-updater.io/previous-resources` annotations plus a
`ResourcesUpdated` Event per change. Argo CD tracking metadata is stripped from
live CRs (the `argocd.argoproj.io/tracking-id` annotation;
`app.kubernetes.io/instance` only when an `argocd*` field manager owns it;
operator-set options like `sync-options` stay). The chart grants the extra RBAC
(create/patch/delete on `resourceoverrides`, create/patch on `events`) only in
this mode — cluster-wide, since namespaces opt in at any time; see the README for
the blast radius. Default stays `gitops`.

**Added — guardrails (all off by default, all overridable per namespace /
workload):**

- `config.maxChangePercent` — each request/limit moves at most N% of its current
  value per sync (live CR, else pod-template value, else no clamp). OOM-driven
  memory growth is exempt upward but still respects `maxMemoryLimitMi`.
- `config.cooldownHours` — a workload changed less than N hours ago keeps its
  values; a fresh OOM still bumps memory. Reads the new `last-changed`
  annotation, which gitops-mode CRs now carry too.
- `config.emitCpuLimits: false` — requests-only CPU: `limits.cpu` is never
  written and is dropped from existing CRs; a CR left with no limits carries an
  explicit `limits: {}` so the webhook removes the pod's template limits too.
- `cronjob.suspend` — chart kill switch for the sync CronJob.
- `webhook.autoRollout.maxConcurrent` (default `1`), `minIntervalSeconds`
  (default `0`) and `rolloutTimeoutSeconds` (default `600`) — due restarts
  queue instead of firing together; a restart counts until its rollout
  completes, the timeout passes, or 3 status reads in a row fail. The rollout
  thread survives transport errors and workloads without a status yet.
- `config.mr.workBranch` — configurable MR/PR source branch under
  `resource-updater/` (it is force-pushed), so several clusters can share one
  gitops repo.

**Changed — autoRollout ignores replays.** A restart fires only when a CR's
resources differ from the last state the webhook knew; watch replays and the
re-list after a reconnect seed that state instead of restarting workloads.

**Changed — the crash-loop gate fails closed.** Without
`kube_pod_container_status_restarts_total` (no kube-state-metrics, or a failed
query) containers are held with reason `no-restart-signal` instead of being
sized from samples the gate cannot vouch for. A query error is retried once
first, and the hold names the case (query failed vs no data). **Upgrade note:**
clusters without kube-state-metrics must set `config.maxRestartsInWindow: 0` (or
`healthGateEnabled: false`) to keep recommendations moving; on clusters with it,
a Prometheus outage that outlasts the retry now freezes recommendations for that
sync instead of computing them.

**Fixed:**

- growOnly / shrinkOnly silently did nothing when both `oomDetectionEnabled` and
  `healthGateEnabled` were off — the live CR baseline was never fetched, and the
  read RBAC was tied to OOM detection. Both are now unconditional.
- A push rejected because the branch moved on is rebased and retried once; any
  other push failure (protected branch, permissions) exits 1 with one redacted
  error instead of a traceback that crash-looped the Job.
- A per-workload `dryRun` workload's committed CR doc was dropped from the
  rebuilt namespace file, so the GitOps engine pruned its CR.
- Docs: one install per cluster (the "central multi-cluster CronJob" wording was
  stale); orphan-cleanup scope; GitHub provider status (token auth + PR flow
  validated, GitHub App auth still alpha).

**Upgrade notes:** chart and app version 0.2.0. Defaults keep 0.1.5 behaviour
except the fail-closed crash-loop gate above (including its effect during a
Prometheus outage) and `autoRollout.maxConcurrent: 1` (set `0` for the previous
unlimited restarts). A `config.mr.workBranch` outside `resource-updater/` is
rejected. CRs gain the two audit
annotations on their next change.

## 0.1.5 — Sample-quality gates: don't size from an untrustworthy window (2026-08-01)

**Fixed — recommendations computed from unhealthy pods.**

A workload entered a crash loop; the sync ran while it was looping and computed
its CPU/memory percentiles from the resulting failed-startup bursts instead of
steady-state usage. Nothing checked whether the source pods were healthy during
the sample window, or how many samples backed a percentile, so the math ran on
the noise and produced a `ResourceOverride` far too small for the real workload.
Because the admission webhook re-applies the stored CR values on every restart
and never recomputes, the undersizing sustained itself — throttling and
OOM-adjacent restarts every few minutes, indefinitely.

Two gates now run per container **before** the value queries:

- **Crash-loop** — `increase(kube_pod_container_status_restarts_total[window])`
  over the widest request window, held above `config.maxRestartsInWindow`
  (default 3). Window-scoped on purpose: the live pod API's `restartCount` is
  cumulative over the container's lifetime and vanishes with the pod.
- **Data sufficiency** — `count_over_time` mirroring the request queries' own
  series and subquery step, held below `config.minSampleCoverage` (default
  0.25) of the window's evaluation points.

When either trips, the workload keeps flowing through the write-back layer and
re-emits the values already committed to its `ResourceOverride`. It is **not**
skipped — skipping would drop the CR doc from the rebuilt file, ArgoCD would
prune the CR, and the workload's pods would fall back to deployment-spec
resources. A container with no committed CR yet is omitted instead. OOM bumps
still apply to a held container (they read the trap limit from the pod spec,
not from Prometheus). Every hold logs `[health-hold]` at WARNING and appears in
a **Held back** table in the MR description.

Both gates fail open when their metric is absent — the crash-loop one warns
once per sync that it is inert without kube-state-metrics. Registered as an
open gap in [ROADMAP.md](ROADMAP.md).

New config, all per-namespace and per-workload overridable through the usual
`helm < namespace < workload` hierarchy: `config.healthGateEnabled`,
`config.maxRestartsInWindow`, `config.minSampleCoverage`.

**Not yet live-validated.** The cluster this ran on removed the tool as the
response to the incident above, so this release was verified offline only: the
full QA suite (62 new asserts in `section_health_gate`, including a replay of
the incident through `_build_containers_payload`), `ruff`, `helm lint`, and the
ConfigMap render. The shapes that only a real backend can confirm — the label
set on `kube_pod_container_status_restarts_total`, `count_over_time` against
real retention and scrape gaps, the *Held back* table in a real MR — have not
been exercised. The two thresholds are reasoned defaults, not measured ones:
`maxRestartsInWindow: 3` treats a single eviction or node drain in a multi-day
window as noise, and `minSampleCoverage: 0.25` is ~2 days of history at the
default 8d memory window. Expect to tune both against your own cluster.

## 0.1.4 — GitHub App authentication (alpha) (2026-06-21)

- **GitHub App authentication** for the `github` provider — an alternative to a
  long-lived PAT, for GitHub-native / SOC2 orgs that can't hand out a personal
  token. Set `git.appId` + `git.installationId` + `git.appPrivateKeySecret`; the
  tool signs a short-lived App JWT (RS256, via the existing `cryptography` dep —
  no new dependency) and exchanges it for an installation access token at sync
  start. GHES via `git.apiUrl`. `config.validate` and the chart's `validate.yaml`
  both reject partial config, token + App dual-mode, and App + `gitProvider=
  gitlab`; the `createMr` gate accepts App auth as valid credentials.
  **Alpha** — the token exchange and the "App not installed" 401/403 path are
  mock-tested only, not yet validated against a live GitHub App.

## 0.1.3 — Artifact Hub: values schema, cosign signing, Bitnami-style README (2026-06-21)

Packaging and documentation release; no behavior change to the tool.

- **Values schema** — the chart now ships `values.schema.json`, so `helm install`
  validates values locally and Artifact Hub shows the *values-schema* badge. It is
  permissive by design (matches the stringly-typed `config.*` knobs) — it rejects
  wrong types and bad enums while accepting every shipped default.
- **Signed charts** — `release.yml` signs the pushed OCI chart with cosign
  (keyless, via GitHub Actions OIDC), so Artifact Hub shows the *Signed* badge.
  Verify with `cosign verify ghcr.io/mateus-gsilva/charts/kube-resource-updater:<version>
  --certificate-identity-regexp 'https://github.com/mateus-gsilva/kube-resource-updater/.*'
  --certificate-oidc-issuer https://token.actions.githubusercontent.com`.
- **Bitnami-style README** — restructured into the standard chart layout (TL;DR,
  Introduction, Prerequisites, Installing/Uninstalling, Architecture, Parameters,
  signature verification, License) with a full Parameters table. `image.*`,
  `global.*`, and the pod/container security contexts gained `@param` annotations
  so the table is exhaustive.

## 0.1.2 — Token-leak fix, clearer 403 hint, webhook Deployment annotations (2026-06-12)

- **Security: the git token no longer leaks in tracebacks.** When a `git`
  subprocess failed, the raised `CalledProcessError.cmd` still carried the auth
  URL (with the token); an unhandled exception printed it in pod logs even
  though the error log was already redacted. `_run` now scrubs the exception's
  `cmd` before re-raising.
- **Clearer 401/403 auth hint for fine-grained PATs.** The GitHub hint named only
  classic-PAT scopes (`repo`/`public_repo`); it now also names the fine-grained
  `Pull requests` + `Contents` permissions — a token with only `Contents` pushes
  the branch but 403s on the Pulls API.
- **New `webhook.deploymentAnnotations`** — set annotations on the webhook
  Deployment's own metadata (e.g. `configmap.reloader.stakater.com/reload` so
  Stakater Reloader rolls the webhook when its config ConfigMap changes).
  `podAnnotations` only reaches the pod template, which Deployment-level
  controllers ignore.

## 0.1.1 — Public-repo cleanup (2026-06-12)

Documentation and packaging cleanup; no behavior change.

- **Chart README ships in the package** — `.helmignore` no longer excludes
  `README.md`, so Artifact Hub and `helm show readme` display it.
- **Internal archaeology stripped** from comments, docstrings, and the QA suite
  (references to internal chart versions, audit findings, and dev phases that
  carried no meaning outside the original development monorepo).
- **Internal-only docs removed** — pre-release config-interaction audits, the
  internal integration test plan, and the planned-features sketches.

## 0.1.0 — Initial public release (2026-06-11)

First public cut, extracted from the original development monorepo with a fresh
history. Version numbering restarts at 0.1.0; the 0.x series signals that the
public packaging (image registry, chart registry, GitHub PR live-validation) is
still settling — the code itself has been running in production since early
2026.

What's included:

- **Sync CronJob** — discovers opted-in namespaces (`kube-resource-updater.enabled: "true"`
  annotation), queries Prometheus for CPU/memory usage (percentile over window,
  plus spike-catching max for limits), applies margins/multipliers/bounds/rounding,
  and writes one `ResourceOverride` CR per workload to a git repo — as a merge
  request (default) or a direct push.
- **Mutating admission webhook** — patches `pod.spec.containers[*].resources` at
  admission time from matching `ResourceOverride` CRs (label-selector matching,
  per-container). Fails open (`failurePolicy: Ignore`); never blocks a pod.
- **Validating admission webhook** — rejects CRs with empty selectors,
  `matchExpressions`, or selector overlaps that would make admission patching
  non-deterministic.
- **`ResourceOverride` CRD** (`kube-resource-updater.io/v1`) with CEL validation
  (cross-field request≤limit checks, quantity-suffix sanity) enforced at the
  apiserver.
- **OOM-aware bumping** — detects `OOMKilled` events at sync time, bumps the
  memory limit by `oomBumpFactor`, records sticky floors as CR annotations, and
  flags repeat offenders for human investigation after 5 consecutive bumps.
- **In-process cert reconciler** — self-signed CA + serving cert generation,
  rotation, and webhook `caBundle` patching. No cert-manager dependency.
- **Auto-rollout (opt-in)** — debounced `kubectl rollout restart`-equivalent on
  workloads whose CR resources changed.
- **Status writer** — stamps `status.lastAppliedAt` / `patchedContainers` /
  `Applied` condition on CRs that actually patched pods.
- **Git providers** — GitLab (merge requests; production-tested) and GitHub
  (pull requests; implemented and mock-tested, not yet live-validated — alpha).
  Provider auto-detected from the repo URL.
- **Helm chart** under [`charts/kube-resource-updater`](charts/kube-resource-updater/)
  with render-time validation gates, optional ServiceMonitor / PrometheusRule /
  NetworkPolicy / PDB.
- **Offline QA suite** (`tools/qa_params.py`) — ~1,250 assertions covering the
  recommendation math, parsing, rendering, webhook patch/validate logic, chart
  renders, and git write-back flows. Runnable via `pytest` (thin wrapper) or
  directly.
- **CI/CD** — GitHub Actions runs `ruff` + the QA suite + `helm lint` on every
  push and pull request; a separate workflow builds the image and publishes it
  to GHCR (`ghcr.io/mateus-gsilva/kube-resource-updater`, tagged with the
  version and `sha-<short>`). The package is private while this repo is private.

Known gaps at this release: see [ROADMAP.md](ROADMAP.md).
