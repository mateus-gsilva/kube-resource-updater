# Changelog

Release history for `kube-resource-updater`. Pending work lives in [ROADMAP.md](ROADMAP.md).

---

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
