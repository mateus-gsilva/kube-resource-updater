# Planned features

Design documents for big features sketched but not started. Each entry includes problem statement, proposed approach, and open questions. When work begins, move the entry to [ROADMAP.md](../ROADMAP.md) Backlog with an entry-state of "in progress"; on ship, move to [CHANGELOG.md](../CHANGELOG.md).

---

## Runtime-aware parameter injection

**Problem:** Many runtimes do not respect cgroup memory limits by default. A JVM without `-XX:MaxRAMPercentage` reads total node memory and sizes the heap accordingly — causing OOMKill in containers with tight limits. Node.js V8 and Go GC have the same issue.

**How StormForge does it (JVM only):**
- Detection: explicit pod labels + JMX metrics endpoint (not automatic)
- Collects GC metrics, heap/non-heap usage, Metaspace, Code Cache over time
- Uses ML to derive heap bounds and GC settings
- Injects `STORMFORGE_JAVA_ARGS` env var (or patches `JAVA_TOOL_OPTIONS`) with flags:
  - `-XX:MaxRAMPercentage=75` — heap ceiling as % of container memory limit
  - `-XX:InitialRAMPercentage=50` — avoids slow startup from small initial heap
  - `-XX:MinRAMPercentage=25` — lower bound for very small containers
  - GC selection: G1GC for multi-CPU + ≥1792Mi; SerialGC for single-CPU or tiny containers
  - `-XX:+ExitOnOutOfMemoryError` — fast-fail instead of thrashing
- Adjusts memory request/limit to account for off-heap (Metaspace + Code Cache + stack)

**Known effective flag set (production-validated):**
```
-XX:+UseContainerSupport
-XX:InitialRAMPercentage=30.0
-XX:MaxRAMPercentage=70.0
-XX:ActiveProcessorCount=<cpu_limit_ceil>
-XX:TieredStopAtLevel=1
-XX:+ExitOnOutOfMemoryError
```
Notes per flag:
- `UseContainerSupport` — default since JDK 8u191 but worth being explicit; without it JVM reads node RAM not cgroup limit
- `InitialRAMPercentage=30` — conservative start; leaves headroom for off-heap at pod init
- `MaxRAMPercentage=70` — keeps 30% for Metaspace + Code Cache + thread stacks (typically 200–400Mi extra)
- `ActiveProcessorCount` — **needs evaluation**: hardcoding to 1 means fewer GC threads, may activate SerialGC instead of G1GC even on multi-core containers, fewer JIT compiler threads; should likely be `ceil(cpu_limit)` computed from the container CPU limit — but `=1` is currently used in production (MTE dev) and may be intentional for startup-optimized small containers
- `TieredStopAtLevel=1` — **needs evaluation**: disables C2 (server) compiler; reduces JIT CPU spikes at startup but caps sustained throughput; good for batch/tiny containers, risky for high-throughput long-running services; currently used in production (MTE dev) → evaluate whether it should be default, opt-in via annotation, or excluded from the recommended set
- `ExitOnOutOfMemoryError` — crash fast instead of thrashing under memory pressure

**Proposed approach for kube-resource-updater:**

1. **Language detection** — scan container image name and command/args:
   - `openjdk`, `temurin`, `jdk`, `jre`, `java -jar` → JVM
   - `node`, `nodejs`, `npm`, `node server.js` → Node.js
   - image is `golang` or binary with `GOMEMLIMIT` already set → Go
2. **Compute safe env var values** from the container's computed memory limit:
   - JVM: `JAVA_TOOL_OPTIONS=-XX:MaxRAMPercentage=75 -XX:InitialRAMPercentage=50 -XX:+ExitOnOutOfMemoryError`
   - Node.js: `NODE_OPTIONS=--max-old-space-size=<limit_mi * 0.75>`
   - Go: `GOMEMLIMIT=<limit_mi * 0.90>MiB`
3. **Write back via MR** — same mechanism as resource requests/limits; never force-apply, always open a MR so the owner can review; skip if the env var is already set on the container
4. **Annotation opt-in/skip** — `kube-resource-updater.runtime: jvm|nodejs|go|skip` to override auto-detection or exclude a workload from runtime patching

**Open questions:**
- Should runtime injection be a separate sync command (`sync --runtime`) or part of `sync`?
- How to detect if app already manages JVM flags (e.g. via `_JAVA_OPTIONS`, `JVM_OPTS`)?
- Off-heap budget for JVM: Metaspace + Code Cache typically adds 200–400Mi; should the tool inflate the memory limit automatically to preserve headroom?

---

## Startup profiling via Metrics Server

**Problem:** New deployments have no Prometheus history. Apps with heavy initialization (JVM warmup, cache preload, migrations) are undersized on first deploy.

**Proposed solution:**
1. Detect new deployment — no existing resource history in git
2. Poll Metrics Server every N seconds during a configurable window (e.g. 5–10 min)
3. Capture peak CPU and memory during startup phase
4. Write initial requests/limits to git based on observed peak
5. Hand off to Prometheus steady-state recommendations once history accumulates

**Open questions:**
- How to detect "new deployment" reliably — compare git SHA? pod annotation?
- How long is "startup"? Configurable per-app annotation?
- How to distinguish startup spike from sustained load?
