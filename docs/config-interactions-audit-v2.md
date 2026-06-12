# Config interaction audit — extended pass (post chart 1.20.0)

Follow-up to [`config-interactions-audit.md`](config-interactions-audit.md)
(2026-05-09, chart 1.13.0). That audit found 9 items, all resolved or
documented in chart 1.14.0+. This pass goes deeper — systematically
enumerating every PAIR / TRIPLE of toggles that the code allows but
produces nonsensical or silent-broken state.

Methodology: read every `if cfg.X:` branch in `main.py` and `src/*.py`,
list the toggle pairs that intersect, then classify each pair.
Findings grouped by severity. Status tagged per item:

- 🟥 **silent-broken**: tool accepts, produces wrong result, no warning. Fix.
- 🟧 **silent-allow**: tool accepts dead config (e.g. unused setting). Warn.
- 🟨 **runtime-warn**: already warns, but only at sync time — could be earlier.
- 🟩 **validated**: already caught by Config.validate / validate.yaml.

---

## A. Toggle pair interactions

### A1 🟥 `dryRun=true` + `createMr=true` — silently opens MR

**Evidence:** `src/writeback_webhook.write_back_webhook_all` early-exits
on `dry_run` (line 147) BEFORE the createMr bucket decision. So dryRun
correctly short-circuits MR creation. ✅ Actually **validated** — re-classify as 🟩.

After re-reading: `if dry_run: ...; return None` at line 147 happens
BEFORE the bucket logic at line 1476. Correctly suppresses MR open.
Move on.

### A2 🟥 `dryRun=true` + `autoRollout=true` — should webhook fire rollouts during dry-run?

**Evidence:** `dryRun` is a CronJob (sync) flag. The webhook is a separate
process (Deployment). The webhook doesn't read `cfg.dry_run` at all —
it reacts to CR `ADDED` / `MODIFIED` events. So if the CronJob is in
dryRun (doesn't write CRs), the webhook has nothing to react to, and
the question is moot. ✅ **Validated by construction.**

### A3 🟧 `oomFloorReset=true` + `oomDetectionEnabled=false` — reset what?

**Evidence:** `src/overrides.is_oom_floor_reset_requested` resolves the
annotation through helm < ns < workload and returns a bool. The reset
is then applied in `_build_containers_payload` (line ~738). But the
reset only fires when `prior_floor or prior_last_event or prior_history`
is non-empty — and those are written only when `oom_detection_enabled`
was true at some point. If detection has been off since install, the
reset annotation has no effect — but doesn't warn.

**Severity:** dead config that an operator might set thinking it does
something. Silent.

**Fix:** at sync time, log a single `[oom-floor-reset] no prior state to
clear (oomDetectionEnabled is off)` warning when the reset annotation is
set but `oom_detection_enabled` resolves to false for that workload.

### A4 🟧 `oomFloorEnabled=true` + `oomDetectionEnabled=false` — floor toggle is dead

**Evidence:** the floor only gets written when a bump fires. If detection
is off, no bump ever fires, so `oom_floor_enabled` resolves to `true`
but does nothing.

**Severity:** dead config. Same surface as A3.

**Fix:** Config.validate warning when `oom_detection_enabled=false` AND
any of `oom_floor_enabled` / `oom_floor_reset` are explicitly set
(non-default). The annotation resolver makes "explicit" hard to detect
at startup, but we can at least warn when the CLUSTER-LEVEL helm
defaults are inconsistent.

### A5 🟧 `createMr` per-workload annotation overrides helm BUT `Config.validate` only checks helm-level token

**Evidence:** `Config.validate` checks `self.create_mr` (helm level) ×
`self.gitlab_token`. If helm `createMr=false` but a workload annotation
sets `createMr=true` AND no token, the validate passes BUT the sync
crashes on that workload's MR-open with 401.

**Severity:** real silent-broken case. Edge — most installs set both
at helm level — but reproducible.

**Fix:** the failure mode is per-WORKLOAD, not per-cluster, so
Config.validate can't fully prevent it (it doesn't know namespace/workload
annotations until sync time). Best fix: extend the **runtime check** in
`_commit_repo` — if any MR-bucket entry exists AND token is empty, fail
with the same clear message as validate, before clone+push starts.

### A6 🟧 MR metadata fields set (`reviewers`, `labels`, `assignees`, `squash`) + `createMr=false`

**Evidence:** MrConfig fields (assignees, reviewers, labels, squash,
remove_source_branch) are only used in `_create_gitlab_mr` and
`_resolve_gitlab_user_ids`. When createMr=false cluster-wide AND no
per-workload override flips to true, none of these fields are ever
consulted. Operators who set them expecting them to apply silently get
nothing.

**Severity:** dead config. Operator-surprising — they set assignees, no
MR opens, no warning, they don't know why.

**Fix:** Config.validate warning when `create_mr=false` AND any
MrConfig field is non-empty. Log at INFO at startup, not error — the
MR config might still apply via workload-annotation `createMr=true`.

### A7 🟨 `roundValues=true` + `cpuLimitMultiplier=4` — order matters, may invariant-break

**Evidence:** `_build_container_resources` at line 282-296 (writeback.py):
```python
if cpu_req_str:
    req_mc = _parse_cpu_m(cpu_req_str)
    if b.min_cpu_request_m > 0: req_mc = max(req_mc, b.min_cpu_request_m)
    if b.round_values: req_mc = _round_up_nice(req_mc)
    if b.max_cpu_request_m > 0: req_mc = min(req_mc, b.max_cpu_request_m)
    reqs["cpu"] = f"{req_mc}m"
    if cpu_limit_m is not None:
        lim_mc = max(cpu_limit_m, b.min_cpu_limit_m)
    else:
        cap_mc = max(round(req_mc * b.cpu_cap_mult), b.min_cpu_limit_m)
        ...
    if b.round_values: lim_mc = _round_up_nice(lim_mc)
    if b.max_cpu_limit_m > 0: lim_mc = min(lim_mc, b.max_cpu_limit_m)
    lim_mc = max(lim_mc, req_mc)  # limit must always be >= request
    limits["cpu"] = f"{lim_mc}m"
```

The order is: round-req → ceil-req → compute-lim-from-req → round-lim →
ceil-lim → enforce lim ≥ req. The `lim_mc = max(lim_mc, req_mc)` line
catches the corner case where `maxCpuLimitM < req_mc` (limit ceiling
below request). Without that line: `cap_mc` after ceiling might be <
req, violating lim ≥ req invariant.

**Severity:** explicitly guarded by `lim_mc = max(lim_mc, req_mc)`. ✅
**Validated by code.** Add a QA assert to lock this in.

### A8 🟥 `skipContainers` includes EVERY container of a workload — workload becomes empty

**Evidence:** `_filter_skipped_containers` in writeback_webhook.py returns
`(kept, dropped)`. If `kept` is empty, the rest of `_build_containers_payload`
loops zero times. The function returns `([], oom_annotations)` (empty
payload, possibly non-empty annotations).

In `_build_entries` line ~277: `if not containers_payload: continue` —
the workload is dropped from `entries`. Net effect: workload silently
un-managed (its CR file in git gets ArgoCD-pruned eventually).

**Severity:** subtle. An operator who sets `skipContainers: "main"` on
a single-container workload (calling the only container "main") gets the
same silent un-management as `skip: "true"`. They probably meant
something different.

**Fix:** at sync time, log a `[skip-containers] all containers of <ns>/<wl>
filtered — workload effectively unmanaged (this is equivalent to
skip=true)` warning when `kept == []` and `dropped != []`. One line per
affected workload.

---

## B. Numeric edge cases

### B1 🟨 `cpuPercentile=0.0` (exactly at lower bound) — Prom returns minimum

**Evidence:** `Config.validate` allows `0.0 <= cpu_percentile <= 1.0`. The
PromQL `quantile_over_time(0, ...)` returns the MINIMUM value over the
window, which is rarely what an operator wants for "recommend cpu
request". But it's a valid statistical operation, not a bug.

**Severity:** legitimate use case (worst-case sizing) but easy to set
by accident. Document, don't warn.

### B2 🟨 `cpuPercentile=1.0` — Prom returns maximum

Same as B1 but max. Useful for "recommend cpu limit", not for request.
Document.

### B3 🟥 `cpuRequestWindow=0s` — semantic zero, syntactic valid

**Evidence:** regex `^(\d+(s|m|h|d|w|y))+$` accepts `0s`. PromQL
`quantile_over_time(0.95, rate(...)[0s])` returns empty. The sync then
warns "no CPU request data for <ns>/<container>" for every workload —
same as if Prom were unreachable.

**Severity:** subtle. Operator who sets `cpuRequestWindow: "0s"`
thinking it disables CPU sizing gets the "no data" path instead of a
clear error.

**Fix:** Config.validate reject windows where the numeric portion is `0`.
Regex group `(\d+)(s|m|h|d|w|y)` with numeric check.

### B4 🟨 `cpuRequestWindow=999y` — exceeds Prom retention

**Evidence:** PromQL accepts arbitrary window. If retention is 30d, the
query effectively asks for "all data" — equivalent to `30d`. No error,
just less data than requested.

**Severity:** harmless. The operator's mental model and the actual Prom
behavior diverge but no harm done. **No fix needed.**

### B5 🟨 `marginFraction=0.0` + `cpuLimitMargin=0.0` — limit = request

**Evidence:** when both margins are 0, the limit ends up `≈ request *
multiplier`. If `cpuLimitMultiplier=1`, then `limit == request`. Pod
admission accepts this but it's borderline broken (no headroom).

**Severity:** valid edge case (someone wants strict limit). No fix
needed.

### B6 🟨 `oomBumpFactor=1.0` — bump that doesn't bump

**Evidence:** when OOM detected, `new_limit = trap × 1.0 == trap`. The
post-bump `_enforce_floors` may still raise via `oom_floor`. But
fundamentally a no-op bump.

**Severity:** range [1,10] allows 1.0. Probably typo (operator meant
"don't bump" but should set `oom_detection_enabled: false`).

**Fix:** Config.validate reject `oom_bump_factor < 1.05` (require
non-trivial bump). Bump the lower-bound check.

---

## C. State-dependent interactions

### C1 🟧 Workload had `skip: "true"`, annotation removed — CR recreated

**Evidence:** The sync regenerates the workload's CR on the next run.
ArgoCD applies the new CR (re-create from git). Pod admission picks up
the new values on next restart. ✅ **Validated by design.**

### C2 🟥 `oomDetectionEnabled` flipped off at cluster level — sticky floors orphaned

**Evidence:** When `oom_detection_enabled=false` resolved at sync time
(line 309 in main.py: `oom_eligibility_lookup[(rec.namespace, rec.target_name)] = eligible`),
the `eligibility` is False, but `_build_containers_payload` STILL reads
`oom_state` from prior CR annotations (line 698: `prev_res_lookup =
(oom_state or {}).get("containers") or {}`). The floor IS still
respected as a memory minimum even with detection off (sticky behavior
per chart 1.12.0 design).

**Severity:** intended behavior, but operator-surprising — flipping the
toggle off doesn't clear floors. Documented in 1.12.0 release notes;
discoverable via `kubectl get ro -A -o yaml | grep oom-floor`.

**Fix:** None code-wise (intended). Reference operator note in docs.

### C3 🟧 Container removed from workload spec — orphan `oom-floor.<old_container>` annotation

**Evidence:** `_build_containers_payload` final annotation merge (line 941):
```python
for container in sorted(out_floor):
    if container not in rendered_names and container not in prior_floor:
        continue  # don't emit annotation for missing container
    oom_annotations[floor_annotation_key(container)] = ...
```

`prior_floor` includes the OLD container's floor. The condition
`container not in rendered_names AND container not in prior_floor` is
the inverse: keep annotation if container EITHER currently renders OR
had prior floor. So an old container's floor STAYS as a CR annotation
even after the container is removed from the workload spec.

**Severity:** stale annotation. Not harmful (the floor doesn't apply to
a container that doesn't exist) but operator-surprising on
`kubectl describe ro`.

**Fix:** at sync time, if container is in `prior_floor` but not in
`rec.containers` (current workload), drop the annotation. Add a debug
log.

---

## D. Chart-level invalid combos

### D1 🟧 `webhook.replicaCount=2` + `webhook.autoRollout.enabled=true` — debouncer races

Already in original audit as #9. Documented; single replica recommended.

### D2 🟧 `webhook.validating.enabled=true` + `webhook.failurePolicy=Fail` — strict rejection on overlap

**Evidence:** ValidatingWebhookConfiguration uses the same
`failurePolicy` as the mutating one. If `Fail`, any
ResourceOverride that the validator can't reach gets REJECTED. With
`Ignore`, conflicts pass through but admission silently fails to
patch.

**Severity:** operator decision, not a bug. Document the trade-off.

### D5 🟥 Empty `matchLabels` selector — silent "ghost" CR

**Evidence:** `webhook_validate.validate` pre-1.21.0 silently allowed CRs with
empty `spec.selector.matchLabels` (the early `_parse` filter returned
`None`, the validator's "candidate is None → allow" path skipped the CR).
The cache's `_parse` also silently dropped it with a `WARNING` (line 304).
End state: CR exists in etcd, `kubectl get ro` shows it, but cache never
indexes it, mutation webhook never patches a pod for it.

**Worse case:** if a future regression or hand-edit modifies the cache's
`_parse` to NOT filter empty selectors, `ResourceOverride.matches()` had a
vacuous-truth bug — `for k,v in {}.items(): ...; return True` matches every
pod in the namespace. Defence-in-depth value.

**Severity:** silent UX failure. Operator who hand-writes a CR with
`matchLabels: {}` (typo or bad copy-paste) sees the CR accepted but no pods
get patched. No error, no warning.

**Fix:** validating webhook rejects CRs with empty/missing `matchLabels`
at admission with a clear message ("set at least one label e.g.
app.kubernetes.io/instance"). Cache's `matches()` adds an explicit
empty-check returning False instead of relying on for-loop vacuous truth.

### D6 🟥 Per-workload `min*RequestM` via annotation > helm-level `max*RequestM`

**Evidence:** `Config.validate` runs once at startup against the HELM-level config — passes when helm `minCpuRequestM=200, maxCpuRequestM=2000`. A workload then annotates `kube-resource-updater.minCpuRequestM: "3000"`. After resolver merge, effective Config has `min=3000, max=2000` — min > max for that workload only. `_build_container_resources` order is "apply min, then apply max" → max wins, req clamped to 2000m. The stated min invariant (3000m) is silently violated.

**Severity:** real silent-broken case. Operator sets a higher floor expecting it to apply; actual value falls below.

**Fix:** detect bound conflicts in the EFFECTIVE Config after resolver merge, in `_build_containers_payload`. When any min > max for the same dimension, SKIP the workload with a `[bounds]` warning that names the conflicting dimension + remediation pointer.

### D7 🟧 `gitlab.token` AND `gitlab.existingSecret` both set — silently picks existingSecret

**Evidence:** `gitlabSecretName` helper picks `existingSecret` first if non-empty, falling back to inline `<fullname>-gitlab`. Secret template renders only when `(not existingSecret) AND token`. So with both set: chart uses external Secret, inline token is rendered nowhere, operator's hand-set value becomes dead config.

**Severity:** dead config + ambiguity.

**Fix:** validate.yaml fail when both are set.

### D8 🟧 Webhook `timeoutSeconds` outside k8s admission spec [1, 30]

**Evidence:** k8s admissionregistration.k8s.io/v1 spec rejects timeouts outside 1..30 seconds, but the chart passes the value through. K8s rejects late with a cryptic message.

**Fix:** validate.yaml range check.

### D9 🟧 `webhook.autoRollout.debounceSeconds < 1` — instant rollout

**Fix:** validate.yaml range check.

### D10 🟧 `cronjob.schedule` empty — CronJob never fires

**Fix:** validate.yaml non-empty check.

### D11 🟥 `crWriteback.path` starting with `/` writes outside the cloned repo

**Evidence:** `_prune_orphan_files` at line 1879 calls `os.path.join(repo_dir, path)`. Python's `os.path.join` discards the first argument when the second is absolute: `os.path.join("/tmp/repo", "/manifests/foo") == "/manifests/foo"`. The tool would then write CR files to the container filesystem root instead of into the cloned repo. Git push fails to commit anything (no changes in the repo dir), or worse — permission errors writing to `/`.

**Severity:** silent-broken. Operator typo (extra leading slash) → CRs disappear into pod filesystem, no MR opens, no clear error.

**Fix:** `Config.validate` + `validate.yaml` both reject leading-slash paths.

### D13 🟥 Cert reconciler multi-replica race on Secret CREATE

**Evidence:** `_regenerate_secret` (`webhook_cert.py:372`) tries `replace_namespaced_secret` → on 404 calls `create_namespaced_secret`. With multiple webhook replicas starting at the same time on a cold cluster (Secret doesn't exist yet), N replicas all generate DIFFERENT random certs, all 404 on REPLACE, all call CREATE. One wins with 201, the others get 409 AlreadyExists — pre-1.21.0 the exception propagated and the reconciler thread crashed. Pod liveness eventually restarted; on second start the Secret existed and `_ensure_secret` returned "existing". Net effect: brief webhook downtime + N-1 wasted certs.

**Worse**: same race on certificate renewal (≈ every 11 months). All replicas check expiry simultaneously, all hit `_regenerate_and_exit` together.

**Severity:** silent availability blip on multi-replica installs. The chart's default `replicaCount: 1` avoids this; the audit-v2-added validate.yaml gate (PDB requires replicaCount≥2) increases the surface where this matters.

**Fix:** on 409 during CREATE, re-read the Secret (whoever wrote it is now source of truth) and adopt their cert. Losing replicas discard their generated cert. Equivalent to restarting + reading existing, but without the crash/restart cycle.

### D14 🟥 Webhook doesn't handle SIGTERM — graceful shutdown skipped on k8s pod termination

**Evidence:** `webhook_server.py:402` catches `KeyboardInterrupt` (SIGINT, ctrl-C) but not SIGTERM. Kubelet sends SIGTERM when terminating a pod. The except block never fires, the `finally` cleanup never runs. Daemons (CR cache, ns cache, status writer, rollout trigger) leak; in-flight admission requests cut abruptly when the kernel kills the process via SIGKILL after `terminationGracePeriodSeconds` (default 30s) expires.

**Severity:** medium — 12-factor disposability violation. Visible at: rolling restarts, node drains, scale-down. Other replicas absorb the gap via failurePolicy: Ignore + the validating webhook short-circuit, but a clean shutdown means in-flight callbacks complete and watches close cleanly.

**Fix:** register SIGTERM + SIGINT handlers via `loop.add_signal_handler` that call `loop.stop()`. The existing finally-block cleanup runs uniformly on both paths. Also dedup an accidental double `rollout_trigger.stop()` call.

### D12 🟥 Derived CR name exceeds k8s 63-char limit

**Evidence:** k8s name-quality requirement: `metadata.name` must be ≤ 63 chars (RFC 1123 DNS label). The chart's CR name comes from `rec.target_name` directly, or `<kind>-<target_name>` for collision-disambiguated cases (chart 1.14.0). The kind prefix is up to 12 chars (`statefulset-`) — workloads with target_name > 51 chars overflow the limit AFTER prefix. The first sync after the collision detection triggers ArgoCD to apply the CR, which the apiserver rejects with `metadata.name: must be no more than 63 characters`. Every subsequent sync logs the same error.

**Severity:** silent-broken at the workload level — operator sees `kubectl get ro -n <ns>` missing the CR for their workload, no clear reason in the sync log.

**Fix:** detect at `_build_entries` time and skip the workload with a `[cr-name-too-long]` warning naming the workload and the rename remediation.

### D3 🟥 `webhook.enabled=true` + `cronjob.enabled=false` — cold start with no CRs

**Evidence:** validate.yaml allows this combo (rejects only when BOTH
are false). But: with no CronJob, no CRs are ever WRITTEN to git. The
webhook starts, watches for CRs that don't exist, and admits every pod
as-is. Operator might expect "webhook protects existing CRs" but if
git has none, there's nothing to protect.

**Severity:** valid use case (operator pre-populates CRs via direct
kubectl apply, then runs only the webhook). But silent. Operators who
disable CronJob without thinking through pre-population get a fully-
running webhook that does nothing.

**Fix:** validate.yaml — when `cronjob.enabled=false` AND
`webhook.enabled=true`, log a `helm install` NOTES.txt message
explaining the cold-start requirement. Not a fail, just a note.

### D4 🟧 `cronjob.concurrencyPolicy=Allow` + git push race

Already documented in original audit (item k). Default Forbid; explicit
override is operator's responsibility.

---

## E. External system states (live-test required, not validatable)

### E1 Prom returns 504 partway through `prefetch_prometheus_parallel`

`src/prometheus.py:60-152` — five `except Exception` handlers
silently return None for the affected query. Other workloads continue
unaffected. ✅ **Validated by design.**

### E2 GitLab returns 503 on MR open

`src/writeback.py:119` — `requests.RequestException` caught, logged,
sync exits with non-zero. ✅ **Validated by design.** Future improvement:
retry-with-backoff before giving up.

### E3 ConfigMap mid-flight reload

The CronJob mounts the ConfigMap once at pod start. Mid-flight ConfigMap
changes don't affect the running sync. ✅ **Validated by design.**

---

## Summary

| Severity | Count | Item refs |
|---|---|---|
| 🟥 silent-broken (real bugs) | 10 | A5, A8, B3, C2 (intended), C3, D5, D6, D11, D12, D13, D14 |
| 🟧 silent-allow (dead config) | 10 | A3, A4, A6, C1 (intended), D1, D2, D3, D4, D7, D8, D9, D10 |
| 🟨 runtime-warn (documentation) | 5 | A7 (guarded), B1, B2, B4, B5, B6 |
| 🟩 validated already | 4 | A1, A2, C1, E1-E3 |

**Fixes shipped in chart 1.21.0:**
1. ✅ **A5** — per-workload `createMr=true` without token → fail before push (writeback layer).
2. ✅ **A8** — `skipContainers` strips all containers → emit unmanage warning.
3. ✅ **B3** — `cpuRequestWindow=0s` → Config.validate reject (helm-time + runtime).
4. ✅ **C3** — orphan `oom-floor.<container>` annotation for removed container → drop on sync.
5. ✅ **D5** — empty `matchLabels` selector → validating webhook rejects + cache `matches()` defensive empty-check.
6. ✅ **D6** — per-workload min > max after annotation merge → skip workload with warning (helm-time check only validates helm-level config).
7. ✅ **D7** — `gitlab.token` AND `gitlab.existingSecret` both set → validate.yaml fail (ambiguity).
8. ✅ **D8** — `webhook.timeoutSeconds` outside [1, 30] → validate.yaml fail (k8s admission spec).
9. ✅ **D9** — `webhook.autoRollout.debounceSeconds < 1` → validate.yaml fail.
10. ✅ **D10** — empty `cronjob.schedule` → validate.yaml fail.
11. ✅ Min > max (helm-level) and zero Prom windows also moved to helm-time validate.yaml (was Config.validate runtime only).

**Secondary** (silent-allow → log INFO/WARN):
- A3, A4: oomFloor* settings dead when oomDetectionEnabled=false.
- A6: MR metadata dead when createMr=false.
- B6: oomBumpFactor=1.0 (effective no-op).

**Documentation gaps** (no code change):
- B1, B2: percentile=0/1 → docs section "stat edge cases".
- D2, D3: webhook failurePolicy + cronjob-disabled mode trade-offs.
