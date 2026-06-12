# Security Policy

`kube-resource-updater` runs as a **cluster-wide mutating admission webhook**
and a CronJob that holds a **git write token** and patches Kubernetes workload
resources. That makes a few areas security-sensitive; we take reports on them
seriously.

## Reporting a vulnerability

**Do not open a public issue for a security report.** Public disclosure before
a fix is available puts every operator running the tool at risk.

Instead, report privately via **[GitHub Security Advisories](https://github.com/mateus-gsilva/kube-resource-updater/security/advisories/new)** —
"Report a vulnerability" on the repo's Security tab. This opens a private
channel visible only to the maintainers.

Please include:

- the version / chart tag and the deployment mode (webhook, CronJob, or both);
- a description of the impact and, if possible, a minimal reproduction;
- whether the issue is already public anywhere.

We aim to acknowledge a report within **3 business days** and to ship a fix or
a documented mitigation within **30 days** for confirmed high-severity issues.
We'll credit reporters in the release notes unless you ask us not to.

## Supported versions

Only the **latest released chart version** receives security fixes. There is no
long-term-support branch yet (single `main`, see
[CONTRIBUTING.md](CONTRIBUTING.md)). Upgrade to the latest tag before reporting
that an old version is affected.

## Security-relevant surface

If you're auditing the project, these are the areas worth your attention:

- **Admission webhook** (`src/webhook_server.py`, `src/webhook_patch.py`,
  `src/webhook_validate.py`) — it sees every pod `CREATE`/`UPDATE` in opted-in
  namespaces and returns JSONPatches. `failurePolicy: Ignore` means a webhook
  outage fails *open* (pods admit unpatched), not closed.
- **RBAC** (`gitops/helm-charts/kube-resource-updater/templates/**/rbac.yaml`,
  `clusterrole.yaml`) — `get/patch` on workload kinds and on the tool's own
  Mutating/ValidatingWebhookConfiguration; the chart narrows these with
  `resourceNames` where possible. New cluster-wide grants are reviewed strictly.
- **Git credentials** (`src/writeback.py`, `src/writeback_webhook.py`) — the
  sync token is read from a Secret, never logged (URLs and command output are
  scrubbed via `_strip_auth` / `_redact_auth` before any log line).
- **In-process serving cert** (`src/webhook_cert.py`) — the webhook generates
  and rotates its own CA + serving cert and patches the webhook configs'
  `caBundle`; no cert-manager dependency.
- **CRD admission validation** (`templates/webhook/crd.yaml`) — CEL rules and
  quantity patterns reject malformed `ResourceOverride` CRs at the apiserver.

## What is *not* a vulnerability

- A down webhook admitting pods with chart-default resources — that is the
  documented `failurePolicy: Ignore` trade-off. Alert on it (the chart ships a
  `PrometheusRule`); it is availability, not a security hole.
- The CronJob opening a Merge Request you then have to review/merge — the
  human review step is intentional (`createMr: true`).
