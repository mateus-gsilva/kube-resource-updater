"""
Process-wide configuration.

Single-cluster, single-Prometheus, single-Git-repo target. Per-namespace and
per-workload tunables live as annotations and are layered on top of this
Config by `src/overrides.py` when each workload is processed. The chart
ConfigMap is the only source of truth for cluster-wide defaults; the
deprecated env-var path stays around as a fallback for local development
(`Config.from_env`) and emits a warning when used in production.
"""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)

CONFIG_FILE = "/etc/kube-resource-updater/config.yaml"


def _bool(val, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes")
    return default


def _parse_csv(val) -> list[str]:
    """Parse a CSV string into a list of stripped, non-empty entries.

    Mirrors `src/overrides._parse_skip_containers` so the helm-default and
    annotation-layer parsers agree on whitespace handling.
    """
    if not val:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return [name.strip() for name in str(val).split(",") if name.strip()]


def _opt_float(val) -> "float | None":
    # Only an absent key (None) or empty string means "unset → fall back to
    # margin_fraction". An explicit 0 is a VALID margin (zero headroom) and
    # must be honored — the old `or val == 0 or val == "0"` arms silently
    # turned a deliberate `cpuRequestMargin: 0` into the 0.10 fallback.
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _int_bound(val, default: int = 0, *, field: str = "") -> int:
    """Convert a ConfigMap value to an int, guarding against YAML bool coercion.

    YAML 1.1 parses unquoted ``true``/``false`` as Python bool. An operator
    who writes ``maxCpuRequestM: true`` (intending to "enable" the feature, not
    knowing that 0 = disabled) gets ``int(True) == 1``, which silently caps
    every workload to 1m CPU — a functional bomb that passes ``validate()``
    because the bounds check short-circuits on ``max_val == 0`` (disabled).

    This helper detects the bool case, emits a WARNING (so the operator sees
    the misconfiguration without an outright crash), and returns ``default``
    (0 = disabled, the safe fallback for all bound fields).
    """
    if isinstance(val, bool):
        _log.warning(
            "[config] %s: value %r is a boolean — YAML parsed an unquoted "
            "'true'/'false'. Numeric bound fields use 0 to mean 'disabled', "
            "not a boolean flag. Treating as %d (disabled). "
            "Quote the value (e.g. '%d') if you intended that exact number.",
            field or "bound field",
            val,
            default,
            int(val),
        )
        return default
    return int(val)


@dataclass
class ResourceConfig:
    """Parameters for resource recommendations (Prometheus source) and global bounds.

    Prometheus is the only resource source the tool supports; the legacy VPA
    path was removed when the webhook architecture landed. The
    `RESOURCE_SOURCE` env var and `resourceSource` ConfigMap key were the
    branch selectors for that path and are now ignored — left as a
    hand-edited ConfigMap they parse to a deprecation warning at startup
    via the unknown-key path, but a freshly-rendered chart no longer emits
    them.
    """
    cpu_percentile: float = 0.90
    mem_percentile: float = 0.90
    cpu_request_window: str = "3d"
    mem_request_window: str = "8d"
    cpu_limit_window: str = "7d"
    mem_limit_window: str = "7d"
    margin_fraction: float = 0.10
    cpu_request_margin: "float | None" = None
    mem_request_margin: "float | None" = None
    cpu_limit_margin: "float | None" = None
    mem_limit_margin: "float | None" = None
    round_values: bool = False
    cpu_limit_multiplier: float = 4.0
    memory_limit_multiplier: float = 3.0
    # global bounds — 0 means disabled
    min_cpu_request_m: int = 0
    min_memory_request_mi: int = 0
    max_cpu_request_m: int = 0
    max_memory_request_mi: int = 0
    max_cpu_limit_m: int = 0
    max_memory_limit_mi: int = 0
    # OOM-aware. When enabled, sync
    # detects OOMKilled events on opted-in workloads at run time and
    # bumps memory limit/request to `pod.limit × oom_bump_factor`.
    # See `src/writeback_webhook._apply_oom_bump`.
    oom_detection_enabled: bool = True
    oom_bump_factor: float = 1.5
    # Floor stickiness. When False, fresh OOMs still bump
    # this sync's CR but the result is NOT recorded as a sticky floor on
    # the CR — subsequent syncs return to Prom-driven sizing. Toggleable
    # per-workload via annotation `kube-resource-updater.oomFloorEnabled`.
    oom_floor_enabled: bool = True
    # Minimum CPU for cold-start OOM path. When Prometheus has
    # no history for a container and a fresh OOM event forces a synthesized
    # resource block, this value replaces the bare 1m default, preventing
    # immediate CPU throttle and readiness failure on restart.
    # Configurable via env COLD_START_CPU_FLOOR_M or ConfigMap coldStartCpuFloorM.
    cold_start_cpu_floor_m: int = 10

    @property
    def effective_cpu_request_margin(self) -> float:
        return self.cpu_request_margin if self.cpu_request_margin is not None else self.margin_fraction

    @property
    def effective_mem_request_margin(self) -> float:
        return self.mem_request_margin if self.mem_request_margin is not None else self.margin_fraction

    @property
    def effective_cpu_limit_margin(self) -> float:
        return self.cpu_limit_margin if self.cpu_limit_margin is not None else self.margin_fraction

    @property
    def effective_mem_limit_margin(self) -> float:
        return self.mem_limit_margin if self.mem_limit_margin is not None else self.margin_fraction

    def bounds(self) -> dict:
        return dict(
            min_cpu_request_m=self.min_cpu_request_m,
            min_memory_request_mi=self.min_memory_request_mi,
            max_cpu_request_m=self.max_cpu_request_m,
            max_memory_request_mi=self.max_memory_request_mi,
            max_cpu_limit_m=self.max_cpu_limit_m,
            max_memory_limit_mi=self.max_memory_limit_mi,
            round_values=self.round_values,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "ResourceConfig":
        return cls(
            cpu_percentile=float(d.get("cpuPercentile", "0.90")),
            mem_percentile=float(d.get("memPercentile", "0.90")),
            cpu_request_window=str(d.get("cpuRequestWindow", "3d")),
            mem_request_window=str(d.get("memRequestWindow", "8d")),
            cpu_limit_window=str(d.get("cpuLimitWindow", "7d")),
            mem_limit_window=str(d.get("memLimitWindow", "7d")),
            margin_fraction=float(d.get("marginFraction", "0.10")),
            cpu_request_margin=_opt_float(d.get("cpuRequestMargin")),
            mem_request_margin=_opt_float(d.get("memRequestMargin")),
            cpu_limit_margin=_opt_float(d.get("cpuLimitMargin")),
            mem_limit_margin=_opt_float(d.get("memLimitMargin")),
            round_values=_bool(d.get("roundValues", False)),
            cpu_limit_multiplier=float(d.get("cpuLimitMultiplier", "4")),
            memory_limit_multiplier=float(d.get("memoryLimitMultiplier", "3")),
            min_cpu_request_m=_int_bound(d.get("minCpuRequestM", 0), field="minCpuRequestM"),
            min_memory_request_mi=_int_bound(d.get("minMemoryRequestMi", 0), field="minMemoryRequestMi"),
            max_cpu_request_m=_int_bound(d.get("maxCpuRequestM", 0), field="maxCpuRequestM"),
            max_memory_request_mi=_int_bound(d.get("maxMemoryRequestMi", 0), field="maxMemoryRequestMi"),
            max_cpu_limit_m=_int_bound(d.get("maxCpuLimitM", 0), field="maxCpuLimitM"),
            max_memory_limit_mi=_int_bound(d.get("maxMemoryLimitMi", 0), field="maxMemoryLimitMi"),
            # OOM-aware. Read from env so the chart toggle
            # propagates without re-rendering ConfigMap; default true so a
            # bare install protects against OOM traps out of the box.
            # `bumpFactor < 1.0` would shrink memory on each OOM; clamp
            # at startup with warning to prevent operator footgun.
            oom_detection_enabled=_bool(
                os.environ.get("OOM_DETECTION_ENABLED", "true"), default=True),
            oom_bump_factor=max(
                float(os.environ.get("OOM_BUMP_FACTOR", "1.5")),
                1.0,
            ),
            oom_floor_enabled=_bool(
                os.environ.get("OOM_FLOOR_ENABLED", "true"), default=True),
            cold_start_cpu_floor_m=_int_bound(d.get("coldStartCpuFloorM", 10), default=10, field="coldStartCpuFloorM"),
        )

    @classmethod
    def from_env(cls) -> "ResourceConfig":
        return cls(
            cpu_percentile=float(os.environ.get("CPU_PERCENTILE", "0.90")),
            mem_percentile=float(os.environ.get("MEM_PERCENTILE", "0.90")),
            cpu_request_window=os.environ.get("CPU_REQUEST_WINDOW", "3d"),
            mem_request_window=os.environ.get("MEM_REQUEST_WINDOW", "8d"),
            cpu_limit_window=os.environ.get("CPU_LIMIT_WINDOW", "7d"),
            mem_limit_window=os.environ.get("MEM_LIMIT_WINDOW", "7d"),
            margin_fraction=float(os.environ.get("MARGIN_FRACTION", "0.10")),
            cpu_request_margin=float(os.environ["CPU_REQUEST_MARGIN"]) if os.environ.get("CPU_REQUEST_MARGIN") else None,
            mem_request_margin=float(os.environ["MEM_REQUEST_MARGIN"]) if os.environ.get("MEM_REQUEST_MARGIN") else None,
            cpu_limit_margin=float(os.environ["CPU_LIMIT_MARGIN"]) if os.environ.get("CPU_LIMIT_MARGIN") else None,
            mem_limit_margin=float(os.environ["MEM_LIMIT_MARGIN"]) if os.environ.get("MEM_LIMIT_MARGIN") else None,
            round_values=os.environ.get("ROUND_VALUES", "false").lower() in ("1", "true", "yes"),
            cpu_limit_multiplier=float(os.environ.get("CPU_LIMIT_MULTIPLIER", "4")),
            memory_limit_multiplier=float(os.environ.get("MEMORY_LIMIT_MULTIPLIER", "3")),
            min_cpu_request_m=int(os.environ.get("MIN_CPU_REQUEST_M", "0")),
            min_memory_request_mi=int(os.environ.get("MIN_MEMORY_REQUEST_MI", "0")),
            max_cpu_request_m=int(os.environ.get("MAX_CPU_REQUEST_M", "0")),
            max_memory_request_mi=int(os.environ.get("MAX_MEMORY_REQUEST_MI", "0")),
            max_cpu_limit_m=int(os.environ.get("MAX_CPU_LIMIT_M", "0")),
            max_memory_limit_mi=int(os.environ.get("MAX_MEMORY_LIMIT_MI", "0")),
            oom_detection_enabled=os.environ.get("OOM_DETECTION_ENABLED", "true").lower() in ("1", "true", "yes"),
            oom_bump_factor=max(
                float(os.environ.get("OOM_BUMP_FACTOR", "1.5")),
                1.0,
            ),
            oom_floor_enabled=os.environ.get("OOM_FLOOR_ENABLED", "true").lower() in ("1", "true", "yes"),
            cold_start_cpu_floor_m=int(os.environ.get("COLD_START_CPU_FLOOR_M", "10")),
        )


@dataclass
class MrConfig:
    """Metadata applied to every GitLab Merge Request the tool opens.

    Fields default to "do nothing" — empty string for the CSVs and the
    historical hardcoded `remove_source_branch=True` for that bool. Any
    field left at its default is omitted from the API payload, so a bare
    chart install opens MRs that look identical to the pre-1.7.0 ones.

    Username CSVs (`assignees`, `reviewers`) are resolved to numeric user
    IDs via `GET /api/v4/users?username=…` once per sync — GitLab's MR
    API only accepts IDs. Unknown usernames log a warning and are dropped
    so a typo'd reviewer never blocks the MR from opening.
    """
    assignees: list[str] = field(default_factory=list)
    reviewers: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    squash: bool = False
    remove_source_branch: bool = True


@dataclass
class CrWritebackConfig:
    """Where the tool writes ResourceOverride CR files.

    Required: repo_url and path. branch defaults to 'main'. The chart's
    Helm `required` template function blocks `helm install/upgrade` when
    repo_url or path is empty; this dataclass re-validates at runtime
    (Config.validate()) as defence in depth against ConfigMap edits
    after install.
    """
    repo_url: str = ""
    branch: str = "main"
    path: str = ""


@dataclass
class Config:
    gitlab_url: str
    gitlab_token: str
    gitlab_username: str
    git_author_name: str
    git_author_email: str
    dry_run: bool
    create_mr: bool
    min_cpu_limit_m: int
    min_memory_limit_mi: int
    prometheus_url: str
    resource: ResourceConfig
    cr_writeback: CrWritebackConfig
    mr: MrConfig = field(default_factory=MrConfig)
    grow_only: bool = False
    shrink_only: bool = False
    log_level: str = "INFO"
    log_format: str = "text"
    log_color: str = "auto"
    # Cluster-wide default container-skip list. Empty by default — the
    # project ships no curated sidecar list (vendor names drift; we'd
    # rather operators write the names they actually run).  Per-namespace
    # and per-workload `kube-resource-updater.skipContainers` annotations
    # override this through the same hierarchy as everything else.
    skip_containers: list[str] = field(default_factory=list)
    # Provider-agnostic git credentials.
    # These are the canonical fields — the legacy gitlab_* counterparts above
    # remain populated for backward-compat with anything that reads them.
    # provider construction in main.cmd_sync uses these generic fields.
    git_token: str = ""
    git_provider: str = ""      # ""|"auto"|"gitlab"|"github"
    git_api_url: str = ""       # override; empty → provider default
    git_username: str = "oauth2"

    def validate(self) -> None:
        """Fail fast at runtime when configuration is missing or inconsistent.

        Three categories of check:
          1. Required keys (crWriteback.repoUrl, crWriteback.path) — same
             as before; chart Helm `required` enforces this at install time
             too but a hand-edited ConfigMap can drift past that gate.
          2. Inconsistent bounds (min > max for any dimension) — produces
             values outside the stated invariants; refuse to start.
          3. Out-of-range numerics (percentile not in (0,1), multiplier
             out of [1,100), bumpFactor out of [1,10]) — typo guard;
             `cpuPercentile: "95"` instead of `"0.95"` produces a
             PromQL `quantile_over_time(95, ...)` that returns garbage.
          4. Malformed Prom duration strings — fail at config-load time
             instead of mid-sync with a Prom 400.
          5. `createMr: true` at helm level with `GITLAB_TOKEN` empty —
             every MR open would crash with 401.

        Each failure category logs its specific message + the chart-value
        path to fix, then exits with code 2.
        """
        import re
        import sys

        errors: list[str] = []

        # ── (1) Required keys ─────────────────────────────────────────────
        if not self.cr_writeback.repo_url:
            errors.append("config.crWriteback.repoUrl is empty (required)")
        if not self.cr_writeback.path:
            errors.append("config.crWriteback.path is empty (required)")
        # Leading-slash path is a real footgun:
        # `os.path.join(repo_dir, path)` discards `repo_dir` when `path`
        # is absolute. The tool would try to write to `/<path>/...` on
        # the container filesystem (likely permission-denied at best,
        # writes-into-rootfs at worst) instead of into the cloned repo.
        if self.cr_writeback.path and self.cr_writeback.path.startswith("/"):
            errors.append(
                f"config.crWriteback.path = {self.cr_writeback.path!r} starts with '/' — "
                f"paths are interpreted as repo-relative; a leading slash would "
                f"make Python's os.path.join discard the repo directory and write "
                f"to the container filesystem root. Remove the leading slash."
            )
        # `prometheusUrl` is required — auto-discovery
        # was dropped. `cmd_sync` would fail later with no recommendations
        # anyway; failing here gives the operator a single, actionable
        # error message. The chart's validate.yaml catches this at helm
        # install time too; this is defence-in-depth against hand-edited
        # ConfigMap drift.
        if not self.prometheus_url:
            errors.append(
                "config.prometheusUrl is empty — required "
                "(auto-discovery was removed). Set it to a reachable Prometheus URL."
            )

        # ── (2) Inconsistent bounds ───────────────────────────────────────
        rc = self.resource
        # min > 0 AND max > 0 AND min > max → bug. 0 means disabled.
        for name, min_attr, max_attr in [
            ("CpuRequest",      "min_cpu_request_m",      "max_cpu_request_m"),
            ("MemoryRequest",   "min_memory_request_mi",  "max_memory_request_mi"),
            ("CpuLimit",        "min_cpu_limit_m",        "max_cpu_limit_m"),
            ("MemoryLimit",     "min_memory_limit_mi",    "max_memory_limit_mi"),
        ]:
            # min_cpu_limit_m / min_memory_limit_mi live on Config (cluster
            # global), not ResourceConfig. Pull from the right object.
            min_val = getattr(rc, min_attr, None)
            if min_val is None:
                min_val = getattr(self, min_attr, 0)
            max_val = getattr(rc, max_attr, 0)
            if min_val and max_val and min_val > max_val:
                errors.append(
                    f"config.min{name} ({min_val}) > config.max{name} ({max_val}) — "
                    f"any computed value would be clamped outside the stated bounds"
                )

        # ── (3) Out-of-range numerics ─────────────────────────────────────
        for attr, label, low, high in [
            ("cpu_percentile",          "config.cpuPercentile",        0.0, 1.0),
            ("mem_percentile",          "config.memPercentile",        0.0, 1.0),
            ("margin_fraction",         "config.marginFraction",       0.0, 5.0),
            ("cpu_limit_multiplier",    "config.cpuLimitMultiplier",   1.0, 100.0),
            ("memory_limit_multiplier", "config.memoryLimitMultiplier",1.0, 100.0),
            ("oom_bump_factor",         "config.oomBumpFactor",        1.0, 10.0),
        ]:
            val = getattr(rc, attr)
            if val is None:
                continue
            if not (low <= val <= high):
                errors.append(
                    f"{label} = {val} is out of expected range [{low}, {high}] — "
                    f"common cause: typo (e.g. 95 instead of 0.95 for a percentile)"
                )

        # Per-type margins (optional fields; None = inherit marginFraction).
        for attr, label in [
            ("cpu_request_margin",  "config.cpuRequestMargin"),
            ("mem_request_margin",  "config.memRequestMargin"),
            ("cpu_limit_margin",    "config.cpuLimitMargin"),
            ("mem_limit_margin",    "config.memLimitMargin"),
        ]:
            val = getattr(rc, attr)
            if val is not None and not (0.0 <= val <= 5.0):
                errors.append(
                    f"{label} = {val} is out of expected range [0.0, 5.0]"
                )

        # ── (4) Prom duration string format ───────────────────────────────
        # Prometheus duration grammar: integer + unit (s/m/h/d/w/y), optionally
        # combined (1h30m). Reject anything else at config-load so the operator
        # doesn't see a cryptic PromQL 400 mid-sync.
        prom_duration_re = re.compile(r"^(\d+(s|m|h|d|w|y))+$")
        # a window like `0s` is syntactically valid
        # but semantically empty — every Prom query returns no data, sync
        # warns "no data for <ns>/<container>" everywhere and writes
        # floor-only CRs. Reject the zero-window case.
        nonzero_digit_re = re.compile(r"[1-9]")
        for attr, label in [
            ("cpu_request_window", "config.cpuRequestWindow"),
            ("mem_request_window", "config.memRequestWindow"),
            ("cpu_limit_window",   "config.cpuLimitWindow"),
            ("mem_limit_window",   "config.memLimitWindow"),
        ]:
            val = getattr(rc, attr)
            if val and not prom_duration_re.match(val):
                errors.append(
                    f"{label} = {val!r} is not a valid Prometheus duration "
                    f"(expected format: 1s / 30m / 2h / 7d / 1w / 1y, or combinations like 1h30m)"
                )
            elif val and prom_duration_re.match(val) and not nonzero_digit_re.search(val):
                errors.append(
                    f"{label} = {val!r} is a zero-length window — every Prom query "
                    f"returns no data, sync would write floor-only CRs everywhere. "
                    f"Set a real window (e.g. '3d' for request, '7d' for limit)."
                )

        # ── (5) createMr=true at helm + empty token ──────────────────────
        # Per-workload createMr=false annotation can flip individual workloads
        # to direct-push without needing the token, but if the helm default is
        # true and there's no token, the very next sync that hits an MR-bound
        # workload will crash. Caught at startup for clarity.
        # Check the generic git_token field; legacy gitlab_token
        # is mirrored into git_token during from_file / from_env so both
        # paths are covered by a single check here.
        _effective_token = self.git_token or self.gitlab_token
        if self.create_mr and not _effective_token:
            errors.append(
                "config.createMr is true but no git token is configured — every MR "
                "the tool tries to open will crash with 401. Set git.token "
                "(or legacy gitlab.token / gitlab.existingSecret) in chart values, "
                "OR set config.createMr=false to use direct push (no token needed "
                "for a git remote the pod has write access to)."
            )

        # ── (8) git_provider validation ───────────────────────────────────
        # Allowed: "" (auto), "auto", "gitlab", "github".
        # Anything else is a config error — the factory will refuse it too,
        # but catching it here gives a cleaner startup error with chart-value
        # path context.
        _valid_providers = ("", "auto", "gitlab", "github")
        if self.git_provider not in _valid_providers:
            errors.append(
                f"config.gitProvider = {self.git_provider!r} is not a valid provider. "
                f"Allowed values: 'gitlab', 'github', 'auto' (or empty for auto-detect). "
                f"Check the chart value git.provider."
            )

        # ── (9) Provider / host mismatch WARNING ──────────────────────────
        # If the effective provider resolves to "github" but the repoUrl host
        # is not github.com-family AND no gitApiUrl is set, the factory will
        # build a GitHubProvider pointing at api.github.com — which will 404
        # on every API call.  This is a WARNING not an error because the
        # operator may intentionally be using a github.com-hosted repo whose
        # URL we parse correctly; the mismatch only matters for GHE.
        if self.git_provider in ("github",):
            from urllib.parse import urlparse as _urlparse_p3
            _repo_host = _urlparse_p3(self.cr_writeback.repo_url).hostname or ""
            _is_github_host = (
                _repo_host == "github.com"
                or _repo_host.endswith(".github.com")
            )
            if not _is_github_host and not self.git_api_url:
                _log.warning(
                    "[config] gitProvider resolves to 'github' but repoUrl host %r "
                    "does not look like a GitHub host and gitApiUrl is unset — "
                    "for GitHub Enterprise Server set gitApiUrl "
                    "(e.g. https://github.corp.example.com/api/v3) so the factory "
                    "targets the right API endpoint instead of api.github.com.",
                    _repo_host,
                )

        # ── (6) git refs + identity ────────────────────────────────────
        # `crWriteback.branch` flows into argv positions on `git clone
        # --branch`, `git fetch origin <branch>`, `git push origin <branch>`,
        # and the f-string `origin/{branch}` for `git checkout -B`. argv-list
        # subprocess.run is shell-injection-safe, but git itself parses
        # `--option`-looking values as options regardless of argv position —
        # a branch named `--upload-pack=/tmp/evil` would let an attacker who
        # controls the ConfigMap run arbitrary commands via the upload-pack
        # hook. Reject anything outside a strict git-ref subset.
        branch_re = re.compile(r"^[a-zA-Z0-9._/-]+$")
        b = self.cr_writeback.branch
        if b and (
            not branch_re.match(b)
            or b.startswith("-")
            or b.startswith("/")
            or ".." in b
        ):
            errors.append(
                f"config.crWriteback.branch = {b!r} is not a safe git ref. "
                f"Allowed: alphanumeric, '.', '_', '-', '/'; must NOT start "
                f"with '-' or '/'; must NOT contain '..'. Git's CLI parses "
                f"option-looking refs as options (e.g. '--upload-pack=...'), "
                f"which would let a hand-edited ConfigMap inject git options."
            )

        # gitlab_username: empty is the documented default (oauth2 fallback
        # in _auth_url). Non-empty must match GitLab's
        # username grammar — starts alphanumeric, then alphanumeric / '.'
        # / '_' / '-'. Today _auth_url percent-encodes via quote(), so URL
        # injection is not exploitable; the validator is defence-in-depth
        # and protects log/MR metadata from misleading display.
        username_re = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
        u = self.gitlab_username
        if u and not username_re.match(u):
            errors.append(
                f"config.gitlabUsername = {u!r} is not a valid GitLab username. "
                f"Allowed: starts with alphanumeric, then alphanumeric / '.' / "
                f"'_' / '-'. Leave empty to use the 'oauth2' default."
            )

        # git_author_name: git accepts empty name and silently falls back
        # to the system identity ("user@host"), confusing commit attribution
        # and misattributing MR descriptions.
        name = (self.git_author_name or "").strip()
        if not name:
            errors.append(
                "config.gitAuthorName is empty or whitespace-only — git would "
                "fall back to system identity ('user@host') and commits would "
                "be misattributed. Set a non-empty author name in chart values."
            )
        elif "\n" in self.git_author_name or "\r" in self.git_author_name:
            errors.append(
                f"config.gitAuthorName = {self.git_author_name!r} contains a "
                f"newline — would break the commit trailer format. Use a "
                f"single-line author name."
            )

        # git_author_email: empty/malformed gets accepted by git (which is
        # lenient about RFC 5322) but rejected by GitLab push hooks as a
        # non-existent committer. Catch the obvious cases — empty, missing
        # '@', empty local-part or domain. Not trying to validate full
        # RFC 5322; the goal is "git happy AND GitLab happy".
        email = (self.git_author_email or "").strip()
        if not email:
            errors.append(
                "config.gitAuthorEmail is empty — git accepts it but GitLab "
                "push hooks may reject the commit as having no committer. "
                "Set a non-empty author email in chart values."
            )
        elif "@" not in email:
            errors.append(
                f"config.gitAuthorEmail = {email!r} has no '@' — not a valid "
                f"email address. Use the form 'name@domain'."
            )
        else:
            local, _, domain = email.partition("@")
            if not local or not domain:
                errors.append(
                    f"config.gitAuthorEmail = {email!r} has an empty local-part "
                    f"or domain — not a valid email address. Use 'name@domain'."
                )

        if errors:
            for msg in errors:
                _log.error("[config] %s", msg)
            sys.exit(2)

    @classmethod
    def from_file(cls, path: str = CONFIG_FILE) -> "Config":
        """Load config from a mounted ConfigMap YAML file."""
        try:
            import yaml
            text = Path(path).read_text()
        except OSError as exc:
            raise RuntimeError(
                f"Config file not found at '{path}'. "
                "Mount the kube-resource-updater ConfigMap at that path, "
                "or set CONFIG_FILE env var to point to your config file."
            ) from exc

        raw = yaml.safe_load(text) or {}
        cfg = raw.get("config", {})

        cw_raw = cfg.get("crWriteback") or {}
        mr_raw = cfg.get("mr") or {}

        # ── Generic git credential resolution ───────────────────────────
        # GIT_TOKEN is the canonical env var; GITLAB_TOKEN is a deprecated alias.
        # If only GITLAB_TOKEN is set, emit a one-line WARNING and use it.
        _git_token_file = os.environ.get("GIT_TOKEN", "")
        _gitlab_token_file = os.environ.get("GITLAB_TOKEN", "")
        if not _git_token_file and _gitlab_token_file:
            _log.warning(
                "[config] GITLAB_TOKEN is deprecated; use GIT_TOKEN instead. "
                "Support for GITLAB_TOKEN will be removed in a future release."
            )
            _git_token_file = _gitlab_token_file

        # gitUsername: generic key takes priority, falls back to legacy gitlabUsername.
        _git_username_file = (
            str(cfg.get("gitUsername", ""))
            or str(cfg.get("gitlabUsername", ""))
            or "oauth2"
        )

        return cls(
            gitlab_url=os.environ.get("GITLAB_URL", ""),
            gitlab_token=_gitlab_token_file,
            gitlab_username=str(cfg.get("gitlabUsername", "")),
            git_author_name=str(cfg.get("gitAuthorName", "kube-resource-updater")),
            git_author_email=str(cfg.get("gitAuthorEmail", "noreply@kube-resource-updater.example")),
            dry_run=_bool(cfg.get("dryRun", False)),
            create_mr=_bool(cfg.get("createMr", True)),
            min_cpu_limit_m=_int_bound(cfg.get("minCpuLimitM", 0), field="minCpuLimitM"),
            min_memory_limit_mi=_int_bound(cfg.get("minMemoryLimitMi", 0), field="minMemoryLimitMi"),
            prometheus_url=str(cfg.get("prometheusUrl", "")).rstrip("/"),
            resource=ResourceConfig.from_dict(cfg),
            cr_writeback=CrWritebackConfig(
                repo_url=str(cw_raw.get("repoUrl", "")).strip(),
                branch=str(cw_raw.get("branch", "main")).strip() or "main",
                path=str(cw_raw.get("path", "")).strip().strip("/"),
            ),
            mr=MrConfig(
                assignees=_parse_csv(mr_raw.get("assignees", "")),
                reviewers=_parse_csv(mr_raw.get("reviewers", "")),
                labels=_parse_csv(mr_raw.get("labels", "")),
                squash=_bool(mr_raw.get("squash", False)),
                remove_source_branch=_bool(mr_raw.get("removeSourceBranch", True), default=True),
            ),
            grow_only=_bool(cfg.get("growOnly", False)),
            shrink_only=_bool(cfg.get("shrinkOnly", False)),
            log_level=str(cfg.get("logLevel", "INFO")).upper(),
            log_format=str(cfg.get("logFormat", "text")).lower(),
            log_color=str(cfg.get("logColor", "auto")).lower(),
            skip_containers=_parse_csv(cfg.get("skipContainers", "")),
            # Generic git credential fields
            git_token=_git_token_file,
            git_provider=str(cfg.get("gitProvider", "")).lower().strip(),
            git_api_url=str(cfg.get("gitApiUrl", "")).rstrip("/"),
            git_username=_git_username_file,
        )

    @classmethod
    def from_env(cls) -> "Config":
        """Deprecated: use Config.load() instead. Reads config from environment variables."""
        _log.warning(
            "Config.from_env() is deprecated. Mount the ConfigMap and use Config.load() instead. "
            "Env var config will be removed in the next major release."
        )
        # ── Generic git credential resolution ───────────────────────────
        # GIT_TOKEN is the canonical env var; GITLAB_TOKEN is a deprecated alias.
        # If only GITLAB_TOKEN is set, emit a one-line WARNING and use it.
        _git_token_env = os.environ.get("GIT_TOKEN", "")
        _gitlab_token_env = os.environ.get("GITLAB_TOKEN", "")
        if not _git_token_env and _gitlab_token_env:
            _log.warning(
                "[config] GITLAB_TOKEN is deprecated; use GIT_TOKEN instead. "
                "Support for GITLAB_TOKEN will be removed in a future release."
            )
            _git_token_env = _gitlab_token_env

        # GIT_USERNAME / GITLAB_USERNAME (deprecated alias)
        _git_username_env = (
            os.environ.get("GIT_USERNAME", "")
            or os.environ.get("GITLAB_USERNAME", "")
            or "oauth2"
        )

        return cls(
            gitlab_url=os.environ.get("GITLAB_URL", ""),
            gitlab_token=_gitlab_token_env,
            gitlab_username=os.environ.get("GITLAB_USERNAME", ""),
            git_author_name=os.environ.get("GIT_AUTHOR_NAME", "kube-resource-updater"),
            git_author_email=os.environ.get("GIT_AUTHOR_EMAIL", "noreply@kube-resource-updater.example"),
            dry_run=os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes"),
            create_mr=os.environ.get("CREATE_MR", "true").lower() in ("1", "true", "yes"),
            min_cpu_limit_m=int(os.environ.get("MIN_CPU_LIMIT_M", "0")),
            min_memory_limit_mi=int(os.environ.get("MIN_MEMORY_LIMIT_MI", "0")),
            prometheus_url=os.environ.get("PROMETHEUS_URL", "").rstrip("/"),
            resource=ResourceConfig.from_env(),
            cr_writeback=CrWritebackConfig(
                repo_url=os.environ.get("CR_WRITEBACK_REPO_URL", "").strip(),
                branch=(os.environ.get("CR_WRITEBACK_BRANCH", "main").strip() or "main"),
                path=os.environ.get("CR_WRITEBACK_PATH", "").strip().strip("/"),
            ),
            mr=MrConfig(
                assignees=_parse_csv(os.environ.get("MR_ASSIGNEES", "")),
                reviewers=_parse_csv(os.environ.get("MR_REVIEWERS", "")),
                labels=_parse_csv(os.environ.get("MR_LABELS", "")),
                squash=os.environ.get("MR_SQUASH", "false").lower() in ("1", "true", "yes"),
                remove_source_branch=os.environ.get("MR_REMOVE_SOURCE_BRANCH", "true").lower() in ("1", "true", "yes"),
            ),
            grow_only=os.environ.get("GROW_ONLY", "false").lower() in ("1", "true", "yes"),
            shrink_only=os.environ.get("SHRINK_ONLY", "false").lower() in ("1", "true", "yes"),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            log_format=os.environ.get("LOG_FORMAT", "text").lower(),
            log_color=os.environ.get("LOG_COLOR", "auto").lower(),
            skip_containers=_parse_csv(os.environ.get("SKIP_CONTAINERS", "")),
            # Generic git credential fields
            git_token=_git_token_env,
            git_provider=os.environ.get("GIT_PROVIDER", "").lower().strip(),
            git_api_url=os.environ.get("GIT_API_URL", "").rstrip("/"),
            git_username=_git_username_env,
        )

    @classmethod
    def load(cls) -> "Config":
        """Load config from file if available, fall back to env vars with a deprecation warning."""
        config_file = os.environ.get("CONFIG_FILE", CONFIG_FILE)
        if Path(config_file).exists():
            return cls.from_file(config_file)
        _log.warning(
            "No config file found at '%s'. Falling back to environment variables (deprecated). "
            "Mount the kube-resource-updater ConfigMap to remove this warning.",
            config_file,
        )
        return cls.from_env()
