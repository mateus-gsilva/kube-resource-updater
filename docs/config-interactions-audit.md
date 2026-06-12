# Config interaction audit — pre-OSS release

Pairwise analysis of every Config knob × annotation marker × code branch.
For each finding: **severity**, **evidence** (file:line + behavior), **fix proposal**.

Audit performed 2026-05-09 against chart 1.13.0 + commit `1c86ee0` of the project repo.

**Status update (chart 1.14.0, commit ahead of `34b9a3e`):** items #1, #2, #3, #4, #5, #6 RESOLVED. Items #7, #8, #9 still pending (docs gaps; tracked below).

---

## 🔴 CRITICAL — silent feature breakage

### ~~#1 — `growOnly` / `shrinkOnly` are functionally no-ops~~ ✅ RESOLVED in 1.14.0

Decision: **fixed** (not removed). Operator told tool to respect their policy decision and that's what we did.

Implementation:
- `fetch_oom_state` extended to return `containers` sub-map per CR (apiserver-source `spec.containers[*].{requests,limits}`).
- `_build_containers_payload` pulls `prev_res_lookup` from `oom_state['containers']` and passes to `_apply_grow_shrink`.
- New operation order: compute → OOM bump → sticky floor → **grow/shrink (operator policy)** → final floors/ceilings (hard invariants).
- shrink_only + OOM bump → bump SUPPRESSED (policy wins), `[oom-bump-suppressed]` WARNING logged with how-to-disable, `oom-floor` annotation NOT stamped (truthful), `oom-last-event` advances (dedupe).
- grow_only + OOM bump → bump applied normally (no conflict, bump grows = grow_only allows).
- Both flags active → "freeze" semantics: workload's CR stays at apiserver-source values for every field; `[freeze]` WARNING logged. Previously SKIPPED entirely (which un-managed the workload by removing it from the CR file).
- QA section_grow_shrink expanded with 13 new asserts covering all combinations.

---

### #1-old — `growOnly` / `shrinkOnly` are functionally no-ops

**Severity:** silent breakage. Operator sets the flag expecting bounded mutation; tool ignores it.

**Evidence:** [`src/writeback_webhook.py:670`](../src/writeback_webhook.py#L670)
```python
res = _apply_grow_shrink(res, old_res=None, grow_only=cfg.grow_only, shrink_only=cfg.shrink_only)
```

`old_res=None` is hardcoded. Inline comment admits it:
> "On a fresh install the previous value is None, so grow/shrink are no-ops. Future enhancement: read the prior CR file from the cloned repo and pass it here so multi-cycle grow/shrink behaves identically to the legacy path."

The function in [`src/writeback.py:347-373`](../src/writeback.py#L347):
```python
def _apply_grow_shrink(new_res, old_res, grow_only, shrink_only):
    if not (grow_only or shrink_only) or not old_res:
        return new_res
    ...
```

returns `new_res` unchanged whenever `old_res` is falsy. Since the caller always passes `None`, the function **always** returns unchanged.

Net effect: an operator who sets `growOnly: "true"` on a workload to prevent shrinkage during a noisy Prom period gets no protection — the next sync can still write a smaller value.

The conflict guard in [`main.py:cmd_sync`](../main.py) still fires when both are `True`:
```python
if eff.grow_only and eff.shrink_only:
    _log.warning("[SKIP] %s/%s: grow-only and shrink-only are both active — no value would ever change; remove one", ...)
    continue
```
But that's misleading — the warning implies the individual flags do something. They don't.

**Fix proposal:** thread the prev CR's containers through `_build_entries` → `_build_containers_payload` and pass them as `old_res` to `_apply_grow_shrink`. The data is already read once per sync via `_read_old_docs` for MR-description delta rendering; same call site could populate a per-workload `prev_containers_lookup`. Estimated effort: ~1.5h (wire-up + QA covering the four code branches: grow-only/shrink-only × first-sync/subsequent-sync).

**Alternative:** delete `growOnly` / `shrinkOnly` entirely. Document `minCpu*` / `maxCpu*` bounds as the supported way to clamp recommendations. Cleaner; smaller surface. Operator loses the "I want this workload to never shrink, regardless of Prom" semantic.

**Decision needed:** fix or remove. Either is correct; the current state of silent breakage is not.

---

### ~~#2 — CR name collision when Deployment and StatefulSet share a name in the same namespace~~ ✅ RESOLVED in 1.14.0

Implementation: pre-scan in `_build_entries` detects `(namespace, target_name)` collisions across kinds. Colliding workloads get `cr_name` prefixed with `<kind>-<name>` (e.g. `deployment-shared`, `statefulset-shared`); non-colliding workloads keep the bare workload name. Loud WARNING logged per collision pair with operator-facing remediation ("rename one workload to avoid the prefix"). QA `section_cr_name_collision` (5 asserts) covers: clean / collision-detected / mixed-in-same-sync / cross-namespace-no-collision.

---

### #2-old — CR name collision when Deployment and StatefulSet share a name in the same namespace

**Severity:** real bug. Two CRs produced with `metadata.name == metadata.name`; apiserver rejects the second, or the file ends up with two docs that clobber each other after ArgoCD apply.

**Evidence:** [`src/writeback_webhook.py:241`](../src/writeback_webhook.py#L241)
```python
out.append(WebhookEntry(
    namespace=rec.namespace,
    cr_name=rec.target_name,      # ← kind is not part of the CR name
    ...
))
```

`list_workloads` in [`src/workload.py:108-118`](../src/workload.py#L108) iterates both `list_namespaced_deployment` AND `list_namespaced_stateful_set`. A namespace with `Deployment/foo` + `StatefulSet/foo` produces two `WorkloadRecommendation`s with `target_name="foo"` but different `target_kind`. `_build_entries` creates two `WebhookEntry`s with `cr_name="foo"`. `_render_namespace_file` then emits two YAML docs with `metadata.name: foo` in the same namespace file.

Outcome at apply time: kube-apiserver enforces unique name per namespace per resource kind. CR is namespace-scoped, so two `ResourceOverride/foo` in `my-ns` is rejected — ArgoCD's apply fails for the second. One CR works, the other doesn't.

**Fix proposal:** make `cr_name` include the workload kind when there's a same-name conflict in the same namespace. Two options:
- **Always prefix with kind** (`deployment-foo`, `statefulset-foo`) — predictable but verbose for the 99% case where there's no conflict.
- **Detect at build time** and only prefix the colliding pair, log a warning. Backward-compatible for clean namespaces.

I'd go with **detect-and-prefix-with-warning** — less churn on existing deployments.

**Estimated effort:** ~30min (detection in `_build_entries` + naming helper + QA assertion + log line).

---

## 🟠 VALIDATION GAPS — fail-fast missing

Config errors that should fail at startup with a clear message, but currently silently degrade or crash mid-sync.

> **Status (chart 1.14.0):** ALL four items in this section (#3, #4, #5, #6) resolved by hardening `Config.validate()`. Failure paths covered by QA `section_config_validate` (~22 asserts). Each failure category logs the specific problem + the chart-value path to fix, then `sys.exit(2)`.

### ~~#3 — `minCpuRequestM > maxCpuRequestM`~~ ✅ RESOLVED in 1.14.0

`Config.validate()` now checks `min > max` for all four dimensions (cpuRequest, memoryRequest, cpuLimit, memoryLimit). `min=0` or `max=0` means "disabled" and is ignored. Each violation logs:

```
[config] config.minCpuRequest (500) > config.maxCpuRequest (200) — any computed value would be clamped outside the stated bounds
```

---

### #3-old — `minCpuRequestM > maxCpuRequestM` (or any min > max for the same dimension)

**Severity:** silent inconsistency. Computed value ends up clamped to `max`, then to `min`, ending below the stated min (or above max — depends on order).

**Evidence:** [`src/writeback.py:_build_container_resources:280-326`](../src/writeback.py#L280) applies `min` first, then `max`. With `min=500, max=200`:
```
req_mc = max(req_mc, 500)   # ≥ 500
req_mc = min(req_mc, 200)   # ≤ 200, ending at 200
```
Result: 200, below the stated min of 500.

[`Config.validate()`](../src/config.py#L256) only checks `crWriteback.repoUrl` and `crWriteback.path`. No cross-field validation.

**Fix proposal:** extend `Config.validate()` to fail-fast with:
- `minCpuRequestM > maxCpuRequestM` → exit 2
- `minMemoryRequestMi > maxMemoryRequestMi` → exit 2
- `minCpuLimitM > maxCpuLimitM` → exit 2
- `minMemoryLimitMi > maxMemoryLimitMi` → exit 2

**Estimated effort:** ~15min.

---

### ~~#4 — Out-of-range numeric annotations~~ ✅ RESOLVED in 1.14.0

Ranges enforced at `Config.validate()`: percentiles ∈ `[0, 1]`, margins ∈ `[0, 5]`, multipliers ∈ `[1, 100]`, `oomBumpFactor` ∈ `[1, 10]`. Common typo `cpuPercentile: 95` (instead of `0.95`) now fails clearly:
```
[config] config.cpuPercentile = 95.0 is out of expected range [0.0, 1.0] — common cause: typo (e.g. 95 instead of 0.95 for a percentile)
```

---

### #4-old — Out-of-range numeric annotations

**Severity:** silent bad data. Operator types `cpuPercentile: "95"` (instead of `0.95`); PromQL receives `quantile_over_time(95, ...)` and returns garbage or errors.

**Evidence:** no range check anywhere. `ResourceConfig.from_dict` does `float(d.get("cpuPercentile", "0.90"))` and stores the raw value. Same for `cpuLimitMultiplier`, `oomBumpFactor`, all margins.

**Fix proposal:** validate ranges at `Config.validate()`:
- `cpuPercentile`, `memPercentile` ∈ `(0, 1)` — Prom `quantile_over_time` requires this
- `marginFraction`, `cpu*Margin`, `mem*Margin` ∈ `[0, 5)` — 5× is already a huge headroom; > 5 is almost certainly a typo
- `cpuLimitMultiplier`, `memoryLimitMultiplier` ∈ `[1, 100)` — 1 = limit equals request; 100 = enormous over-provision
- `oomBumpFactor` ∈ `[1, 10]` — already clamped to ≥ 1.0 with warning, but no upper bound

For per-workload annotations (resolved by `overrides.py`), do the same validation in `_KEY_SPEC` parsers — already partially done for `_parse_bool` (falls through to next layer on ValueError); extend `_parse_float` to range-check.

**Estimated effort:** ~45min (validate + QA + log message for each out-of-range case).

---

### ~~#5 — Malformed Prometheus duration strings~~ ✅ RESOLVED in 1.14.0

`Config.validate()` regex-validates `cpuRequestWindow`, `memRequestWindow`, `cpuLimitWindow`, `memLimitWindow` against `^(\d+(s|m|h|d|w|y))+$`. Compound durations like `1h30m` allowed. Bad value:
```
[config] config.cpuRequestWindow = '5days' is not a valid Prometheus duration (expected format: 1s / 30m / 2h / 7d / 1w / 1y, or combinations like 1h30m)
```

---

### #5-old — Malformed Prometheus duration strings

**Severity:** PromQL query fails at first sync of the misconfigured workload. Operator sees a cryptic Prom 400 error in the sync log instead of a clear "your config is wrong" message at startup.

**Evidence:** [`ResourceConfig`](../src/config.py#L63) accepts `cpuRequestWindow: str = "3d"` with no validation. `from_dict` does `str(d.get(...))`. The first time the query runs:
```
GET /api/v1/query?query=quantile_over_time(0.9, ...{5days})
→ 400 Bad Request: parse error
```

Acceptable Prom durations: `^[0-9]+(s|m|h|d|w|y)$` (with optional combinations like `1h30m`).

**Fix proposal:** validate window strings against the regex at `Config.validate()`. Also validate per-workload annotation parsers in `overrides.py` (`cpuRequestWindow`/`memRequestWindow`/`cpuLimitWindow`/`memLimitWindow` all go through `_parse_str`; could pass through a `_parse_duration` instead).

**Estimated effort:** ~30min.

---

### ~~#6 — `createMr: true` cluster-wide + `GITLAB_TOKEN` empty~~ ✅ RESOLVED in 1.14.0

`Config.validate()` now fails fast:
```
[config] config.createMr is true but GITLAB_TOKEN is empty — every MR the tool tries to open will crash with 401. Set gitlab.token or gitlab.existingSecret in chart values, OR set config.createMr=false to use direct push.
```

Per-workload `createMr=false` annotation can still flip individual workloads to direct-push without needing a token — the validate check guards against the cluster default (helm helm-level) which is the most common misconfiguration.

---

### #6-old — `createMr: true` cluster-wide + `GITLAB_TOKEN` empty

**Severity:** sync crashes mid-flight with a 401 from the GitLab API. Operator's CR file gets pushed BUT the MR creation fails, leaving an open branch with no MR. Next sync triggers the 409 path which also fails, leaving a stale branch forever.

**Evidence:** [`src/writeback.py:log_git_credentials_source`](../src/writeback.py#L53) logs a warning if `gitlab_token` is empty but doesn't abort. The sync continues, the `_create_gitlab_mr` POST returns 401, propagates as `requests.HTTPError`, the sync fails.

**Fix proposal:** in `Config.validate()`, if `cfg.create_mr` is True (chart helm default), require `cfg.gitlab_token`. Note: per-workload `createMr` annotation overrides can flip individual workloads to direct-push without token, so the strict check is on the helm-level flag — if the operator wants no-token, they set `config.createMr: false` and `createMr` annotations selectively. Edge case: `createMr: false` helm + `createMr: true` annotation on a workload + no token → that workload's MR fails. Hard to validate at startup (per-workload annotation isn't known then). Live with the mid-sync failure for this case; document it.

**Estimated effort:** ~20min.

---

## 🟡 DOCS GAPS — intentional behavior, but operator-surprising

### #7 — `crWriteback.path` change leaves orphan files in the old path

**Behavior:** orphan cleanup only scans inside the configured `crWriteback.path`. Changing the path leaves all previously-written files under the OLD path. ArgoCD with `directory.recurse: true` would still apply them, so the cluster ends up with CRs from both paths — including stale ones that no longer match any opted-in workload.

**Severity:** docs gap. The tool wouldn't know how to "find the old path" without operator help.

**Fix proposal:** add a warning section to `docs/reference.md` under `crWriteback`: "Changing `crWriteback.path` after first sync leaves orphan files at the old path. Operator must `git rm` them manually or set `directory.recurse: false` on the ArgoCD Application + restrict to the new path."

Could also detect the case: log a WARNING at startup if there are CR-shaped files at unknown paths under the gitops repo's root. But this is invasive and slow (recursive scan of the whole repo). Better as documentation.

---

### #8 — Setting `skip: "true"` on a previously-managed workload silently deletes its CR

**Behavior:** workload that previously had a CR gets removed from this sync's `entries`. `_render_namespace_file` rebuilds the file from `entries` only — the workload's doc isn't included. After commit + ArgoCD apply, the CR is deleted from the cluster. Admission webhook stops patching new pods → next pod restart goes back to deployment-spec resources.

**Severity:** intended ("skip = stop managing") but unannounced. Operator might expect the existing CR to stay frozen at its current values.

**Fix proposal:** add an INFO log at sync time when a workload that has an existing CR (visible via `oom_state_lookup` or by reading the old doc) is now skipped:
```
[SKIP] my-app/api: kube-resource-updater.skip=true — existing CR will be removed from the file
       (admission webhook will stop patching pods; future restarts use deployment-spec resources)
```

Plus a paragraph in `docs/reference.md` Annotations section under `skip`.

**Estimated effort:** ~15min (log message + docs).

---

### #9 — Webhook `replicaCount > 1` + status writer race

**Behavior:** chart default is `replicaCount: 1`. If operator sets to ≥ 2, both replicas independently observe pod admission and stamp `lastAppliedAt` on the same CR. Last write wins. The race is idempotent at the timestamp level (a stamp is a stamp), so the CR ends up with whichever replica was last to PATCH within the 30s flush window.

**Severity:** by design (status is best-effort, single-runner-friendly), but the `replicaCount` chart parameter doesn't document the trade-off.

**Fix proposal:** add a comment in `values.yaml` under `webhook.replicaCount`:
```yaml
## When replicaCount > 1, the status writer's last-write-wins is acceptable
## (timestamp idempotency), but the auto-rollout debouncer maintains
## per-replica state — multi-replica deployments can fire 2× rollouts
## within the debounce window. Single replica recommended.
```

Actually re-read the rollout code carefully — if both replicas schedule the same rollout, the second patch call to the workload is a no-op (same `restartedAt` timestamp, or `kubectl rollout restart` semantics). So this might not be a real concern. **Action: verify in code** before documenting.

---

## 🟢 NON-ISSUES — moot due to upstream bug

These items WOULD be real conflicts if `_apply_grow_shrink` worked. Since it's currently no-op (finding #1), they don't manifest. **Re-evaluate after #1 is fixed.**

- `shrinkOnly` + `minCpuRequestM` — shrink would revert the floor's push-up.
- `growOnly` + `maxCpuRequestM` — grow would revert the ceiling's clamp-down.
- `shrinkOnly` + OOM bump — shrink would revert the bump (urgency-vs-policy conflict).

The right design once #1 is fixed:
1. Apply floors AFTER grow/shrink (current order is grow/shrink → enforce_floors → OOM bump → enforce floor). The order matters; floors should be hard invariants on top of policy clamps.
2. OOM bump should always win (skip grow/shrink for the bump path) — kernel killed the pod, operator policy is secondary.

---

## Summary

| # | Finding | Severity | Effort | Status |
|---|---------|----------|--------|--------|
| 1 | growOnly/shrinkOnly are no-ops | 🔴 silent breakage | 1.5h (fix path chosen) | ✅ 1.14.0 — fixed; shrink_only absolute (respects operator policy even over OOM) |
| 2 | CR name collision Deployment + StatefulSet same name | 🔴 bug | 30min | ✅ 1.14.0 — auto-detected, kind prefix applied to colliders only |
| 3 | min > max bounds not validated | 🟠 fail-fast gap | 15min | ✅ 1.14.0 — Config.validate exit(2) with clear message |
| 4 | Numeric out-of-range not validated | 🟠 fail-fast gap | 45min | ✅ 1.14.0 — percentile/multiplier/margin/bumpFactor all range-checked |
| 5 | Prom duration string not validated | 🟠 fail-fast gap | 30min | ✅ 1.14.0 — regex against Prom duration grammar |
| 6 | createMr+empty-token not validated | 🟠 fail-fast gap | 20min | ✅ 1.14.0 — fail-fast at startup |
| 7 | crWriteback.path change leaves orphans | 🟡 docs gap | 15min | pending |
| 8 | skip workload silently removes CR | 🟡 docs gap | 15min | pending |
| 9 | replicaCount > 1 trade-offs | 🟡 docs gap | varies, verify first | pending |

**Resolved in chart 1.14.0:** items #1-#6 (the actual code-behavior issues). Approx. 40 new QA asserts across `section_grow_shrink`, `section_cr_name_collision`, `section_config_validate`.

**Remaining (docs gaps only):** #7-#9 — operator-facing documentation that the tool's behavior doesn't change but operators should be aware of. Targeted for the OSS-readiness docs sweep.

After these, the tool is in good shape for OSS release from a correctness standpoint. Performance/scale concerns are tracked in the "Pre-release integration test sweep" ROADMAP item.
