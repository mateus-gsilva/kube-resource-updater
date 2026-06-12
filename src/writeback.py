"""
Resource computation and git/GitLab helpers shared with the webhook write-back path
(src/writeback_webhook.py).

This module used to host the legacy Helm/Kustomize value-tree write-back. After the
migration to the ResourceOverride CRD architecture (see docs/webhook-migration.md),
the legacy paths were removed and only the still-shared helpers live here:

  - Git auth + MR creation (`_auth_url`, `_create_gitlab_mr`,
    `_project_path_from_url`).
  - Resource math (`ResourceBounds`, `PromValues`, `_build_container_resources`,
    `_apply_grow_shrink`, `_enforce_floors`, `_round_up_nice`,
    `_fmt_memory`, `_parse_cpu_m`, `_parse_memory_bytes`).
  - Per-container delta formatting (`_fmt_delta_cpu`, `_fmt_delta_mem`, `_delta_str`).
  - Prometheus query bridge (`_query_prom_values`).
"""

from dataclasses import dataclass
from urllib.parse import quote, urlparse

from kubernetes.utils import parse_quantity as _k8s_parse_quantity
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import ResourceConfig
from . import log as _log_module
from .prometheus import (
    query_cpu_max_m,
    query_cpu_request_m,
    query_mem_request_bytes,
    query_memory_max_bytes,
)
from .workload import WorkloadRecommendation

_log = _log_module.get(__name__)


# --------------------------------------------------------------------------- #
# Git / GitLab helpers                                                         #
# --------------------------------------------------------------------------- #

def _auth_url(repo_url: str, token: str, username: str = "oauth2") -> str:
    if token:
        safe_user = quote(username or "oauth2", safe="")
        safe_token = quote(token, safe="")
        return repo_url.replace("https://", f"https://{safe_user}:{safe_token}@")
    return repo_url


def _project_path_from_url(repo_url: str) -> str:
    path = urlparse(repo_url).path.lstrip("/")
    return path[:-4] if path.endswith(".git") else path


def log_git_credentials_state(
    repo_url: str,
    git_token: str,
    git_provider: str = "",
    git_username: str = "oauth2",
) -> None:
    """Log credential resolution state. Call once before any git operations.

    Provider-agnostic: reads cfg.git_token (the canonical Phase-3 field,
    populated from GIT_TOKEN env var). Never reads the legacy
    cfg.gitlab_token — that field stays for backward compat but is NOT
    the authoritative credential source post-Phase-3.

    A WARNING fires ONLY when no token is set AND the write-back path would
    need one (i.e. caller is responsible for only calling this when write-back
    is active). When a token IS present, an INFO line notes the resolved
    provider so the operator can confirm the right backend is selected.

    Do NOT duplicate the GITLAB_TOKEN deprecation warning emitted by
    config.from_env / config.from_file — that warning covers alias-migration;
    this function covers "do we have credentials at all".
    """
    from .git_provider import _detect_provider
    resolved_provider = (
        git_provider.lower().strip()
        if git_provider.lower().strip() in ("gitlab", "github")
        else _detect_provider(repo_url)
    )
    if git_token:
        _log.info(
            "[git] credentials present — provider=%s username=%s",
            resolved_provider, git_username or "oauth2",
        )
        return
    _log.warning(
        "[git] GIT_TOKEN not set — git operations will fail "
        "(set git.token or git.existingSecret in chart values)"
    )


# Module-level retry session for GitLab API calls.
#
# Pre-1.22.10 each `_create_gitlab_mr` call shelled out via bare
# `requests.{get,post,put}` with no transport-level retry. A transient 5xx
# (502/503/504) on POST lost the MR for ~24h (next CronJob); a 429 rate-limit
# crashed mid-sync. urllib3.Retry with `status_forcelist={429,500,502,503,504}`,
# exponential backoff, and `respect_retry_after_header=True` (so GitLab's
# explicit cooldown is honoured) closes both failure modes.
#
# POST is in `allowed_methods` even though RFC 7231 says POST is non-idempotent
# in general: our `_create_gitlab_mr` is idempotent end-to-end (1.22.7 added
# pre-POST adoption GET and 409 race-recovery), so a retried POST that the
# server actually saw the first time recovers via the 409 branch.
#
# `raise_on_status=False` is mandatory — otherwise urllib3 raises on the final
# attempt and the existing `if resp.status_code == 409:` branch never fires.
#
# Lazy singleton so the QA can patch `_GITLAB_SESSION = None` to reset between
# subcases; `_resolve_gitlab_user_ids` deliberately keeps bare `requests.get`
# (degraded-reviewers vs failed-MR trade-off — drop a flaky username, don't
# stall startup retrying lookups).
_GITLAB_SESSION: "requests.Session | None" = None


def _gitlab_session() -> "requests.Session":
    """Return a lazily-initialised `requests.Session` with GitLab retry policy."""
    global _GITLAB_SESSION
    if _GITLAB_SESSION is None:
        retry = Retry(
            total=4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(("GET", "POST", "PUT")),
            backoff_factor=1.0,
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _GITLAB_SESSION = session
    return _GITLAB_SESSION


# Thin wrappers around the retry session so tests can patch at one
# stable symbol (e.g. `patch("src.writeback._gitlab_get")`) without
# poking at session internals. The wrappers replaced the previous bare
# `requests.{get,post,put}` calls in `_create_gitlab_mr` (1.22.10).
def _gitlab_get(url, **kwargs):
    return _gitlab_session().get(url, **kwargs)


def _gitlab_post(url, **kwargs):
    return _gitlab_session().post(url, **kwargs)


def _gitlab_put(url, **kwargs):
    return _gitlab_session().put(url, **kwargs)


def _safe_json(resp, context: str):
    """`resp.json()` but with a clear error on non-JSON 200 responses.

    Defensive JSON handling — a misconfigured proxy / WAF can return
    `200 OK` with an HTML error page; the stock `resp.json()` raises
    `ValueError: Expecting value: line 1 column 1` which gives the
    operator zero idea which call failed or what came back. Re-raise as
    `RuntimeError` with the call context, status code, and a truncated
    body sample so the failure is immediately diagnosable.
    """
    try:
        return resp.json()
    except ValueError as exc:
        body_sample = (resp.text or "")[:200]
        raise RuntimeError(
            f"{context}: GitLab returned non-JSON body "
            f"(status={resp.status_code}, Content-Type={resp.headers.get('Content-Type', '?')!r}): "
            f"{body_sample!r}"
        ) from exc


def _resolve_gitlab_user_ids(
    gitlab_url: str,
    token: str,
    usernames: "list[str]",
) -> "list[int]":
    """Resolve usernames to GitLab numeric user IDs.

    GitLab's MR API only accepts IDs for `assignee_ids` / `reviewer_ids`; the
    chart values let operators write usernames so they don't have to dig the
    numeric ID out of the GitLab UI for each reviewer. Unknown usernames are
    logged and dropped (so a typo on one reviewer never blocks the MR).

    Order is preserved — ID list mirrors the username CSV order — for log
    readability and for matching the operator's intent (first reviewer in
    the list usually maps to the on-call / DRI).
    """
    if not usernames or not token:
        return []
    headers = {"PRIVATE-TOKEN": token}
    out: list[int] = []
    for name in usernames:
        try:
            resp = requests.get(
                f"{gitlab_url}/api/v4/users",
                headers=headers,
                params={"username": name},
                timeout=10,
            )
            resp.raise_for_status()
            users = resp.json() or []
            if not users:
                _log.warning("[mr] username %r not found on %s — dropped", name, gitlab_url)
                continue
            out.append(int(users[0]["id"]))
        except (requests.RequestException, KeyError, ValueError) as exc:
            _log.warning("[mr] username %r lookup failed: %s — dropped", name, exc)
    return out


def _create_gitlab_mr(
    gitlab_url: str,
    token: str,
    project_path: str,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    assignee_ids: "list[int] | None" = None,
    reviewer_ids: "list[int] | None" = None,
    labels: "list[str] | None" = None,
    squash: bool = False,
    remove_source_branch: bool = True,
) -> str:
    encoded = quote(project_path, safe="")
    headers = {"PRIVATE-TOKEN": token}

    payload: dict = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
        "remove_source_branch": remove_source_branch,
        "squash": squash,
    }
    if assignee_ids:
        # `assignee_ids` is the multi-assignee variant (Premium); the API
        # accepts it on Free tier too and silently picks the first one.
        payload["assignee_ids"] = assignee_ids
    if reviewer_ids:
        payload["reviewer_ids"] = reviewer_ids
    if labels:
        # GitLab's MR endpoint expects a comma-joined string, not a JSON array.
        payload["labels"] = ",".join(labels)

    # Pre-POST adoption lookup makes MR-open idempotent across
    # the 4→5 transition of the sync state machine (clone → write → commit
    # → push → MR-open). If a previous sync's POST crashed mid-call after
    # push succeeded, the work-branch is orphaned on the remote with no
    # MR. Re-running would otherwise force-push the same content and POST
    # again; if the prior POST had reached the server before failing,
    # GitLab returns 409 — but a network failure BEFORE the server saw
    # the request leaves no MR, and the operator never learns. Looking up
    # an existing open MR for the exact (source, target) pair recovers
    # both cases. Skipped when source == target (direct-push bucket
    # doesn't open MRs).
    #
    # All HTTP calls carry timeout=30: without it, a hung GitLab API
    # blocks the whole CronJob until cronjob.activeDeadlineSeconds
    # (default 1800s) hard-kills the pod. 30s comfortably covers the
    # 90th-percentile create-MR latency on a healthy self-hosted GitLab (≤2s).
    if source_branch != target_branch:
        list_resp = _gitlab_get(
            f"{gitlab_url}/api/v4/projects/{encoded}/merge_requests",
            headers=headers,
            params={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "state": "opened",
            },
            timeout=30,
        )
        list_resp.raise_for_status()
        existing = _safe_json(list_resp, "MR adoption lookup")
        if existing:
            mr = existing[0]
            _gitlab_put(
                f"{gitlab_url}/api/v4/projects/{encoded}/merge_requests/{mr['iid']}",
                headers=headers,
                json={"description": description},
                timeout=30,
            ).raise_for_status()
            return mr["web_url"]

    resp = _gitlab_post(
        f"{gitlab_url}/api/v4/projects/{encoded}/merge_requests",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if resp.status_code == 409:
        list_resp = _gitlab_get(
            f"{gitlab_url}/api/v4/projects/{encoded}/merge_requests",
            headers=headers,
            params={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "state": "opened",
            },
            timeout=30,
        )
        list_resp.raise_for_status()
        mrs = _safe_json(list_resp, "MR 409 race-recovery lookup")
        if not mrs:
            return "(MR already exists)"
        mr = mrs[0]
        _gitlab_put(
            f"{gitlab_url}/api/v4/projects/{encoded}/merge_requests/{mr['iid']}",
            headers=headers,
            json={"description": description},
            timeout=30,
        ).raise_for_status()
        return mr["web_url"]
    resp.raise_for_status()
    return _safe_json(resp, "MR create POST response")["web_url"]


# --------------------------------------------------------------------------- #
# Resource calculation helpers                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class ResourceBounds:
    """Scalar constraints and multipliers passed to _build_container_resources."""
    cpu_cap_mult: float = 4.0
    mem_cap_mult: float = 3.0
    min_cpu_limit_m: int = 0
    min_memory_limit_mi: int = 0
    min_cpu_request_m: int = 0
    min_memory_request_mi: int = 0
    max_cpu_request_m: int = 0
    max_memory_request_mi: int = 0
    max_cpu_limit_m: int = 0
    max_memory_limit_mi: int = 0
    round_values: bool = False

    @classmethod
    def from_config(
        cls,
        cpu_cap_mult: float,
        mem_cap_mult: float,
        min_cpu_limit_m: int = 0,
        min_memory_limit_mi: int = 0,
        rc: "ResourceConfig | None" = None,
    ) -> "ResourceBounds":
        return cls(
            cpu_cap_mult=cpu_cap_mult,
            mem_cap_mult=mem_cap_mult,
            min_cpu_limit_m=min_cpu_limit_m,
            min_memory_limit_mi=min_memory_limit_mi,
            **(rc.bounds() if rc else {}),
        )


@dataclass
class PromValues:
    """Prometheus-computed resource values for a single container."""
    cpu_request_m: int | None = None
    memory_request_bytes: int | None = None
    cpu_limit_m: int | None = None
    memory_limit_bytes: int | None = None


def _round_up_nice(value: int) -> int:
    """Ceiling to the nearest order-of-magnitude step: 101→200, 1001→2000, 11→20."""
    if value <= 0:
        return value
    if value < 10:    step = 1
    elif value < 100: step = 10
    elif value < 1000: step = 100
    elif value < 10000: step = 1000
    else: step = 10000
    if value % step == 0:
        return value
    return ((value // step) + 1) * step


def _fmt_memory(raw: str) -> str:
    try:
        b = int(raw)
    except (ValueError, TypeError):
        return raw
    GiB = 1 << 30
    if b >= GiB and b % GiB == 0:
        return f"{b // GiB}Gi"
    return f"{round(b / (1 << 20))}Mi"


def _parse_cpu_m(cpu_str: str) -> int:
    """Parse CPU string to millicores."""
    try:
        if cpu_str.endswith("m"):
            return int(cpu_str[:-1])
        return int(float(cpu_str) * 1000)
    except (ValueError, TypeError):
        return 0


def _build_container_resources(
    container,
    bounds: "ResourceBounds",
    cpu_limit_m: int | None = None,
    memory_limit_bytes: int | None = None,
    cpu_request_m: int | None = None,
    memory_request_bytes: int | None = None,
) -> dict:
    b = bounds
    reqs: dict = {}
    limits: dict = {}

    cpu_req_str = f"{cpu_request_m}m" if cpu_request_m is not None else container.target.cpu
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
            if container.upper_bound.cpu:
                ub_mc  = int(container.upper_bound.cpu.rstrip("m"))
                lim_mc = max(min(ub_mc, cap_mc), b.min_cpu_limit_m)
            else:
                lim_mc = cap_mc
        if b.round_values: lim_mc = _round_up_nice(lim_mc)
        if b.max_cpu_limit_m > 0: lim_mc = min(lim_mc, b.max_cpu_limit_m)
        lim_mc = max(lim_mc, req_mc)  # limit must always be >= request
        limits["cpu"] = f"{lim_mc}m"

    mem_req_raw = str(memory_request_bytes) if memory_request_bytes is not None else container.target.memory
    if mem_req_raw:
        try:
            req_b = int(mem_req_raw)
            if b.min_memory_request_mi > 0: req_b = max(req_b, b.min_memory_request_mi * (1 << 20))
            if b.round_values:
                req_b = _round_up_nice(round(req_b / (1 << 20))) * (1 << 20)
            if b.max_memory_request_mi > 0: req_b = min(req_b, b.max_memory_request_mi * (1 << 20))
            reqs["memory"] = _fmt_memory(str(req_b))
            min_mem_b = b.min_memory_limit_mi * (1 << 20)
            if memory_limit_bytes is not None:
                lim_b = max(memory_limit_bytes, min_mem_b)
            else:
                cap_b = req_b * b.mem_cap_mult
                ub_b  = int(container.upper_bound.memory) if container.upper_bound.memory else cap_b
                lim_b = max(round(min(ub_b, cap_b)), min_mem_b)
            if b.round_values:
                lim_b = _round_up_nice(round(lim_b / (1 << 20))) * (1 << 20)
            if b.max_memory_limit_mi > 0: lim_b = min(lim_b, b.max_memory_limit_mi * (1 << 20))
            lim_b = max(lim_b, req_b)  # limit must always be >= request
            limits["memory"] = _fmt_memory(str(lim_b))
        except (ValueError, TypeError):
            reqs["memory"] = _fmt_memory(mem_req_raw)
            limits["memory"] = _fmt_memory(mem_req_raw)

    resources: dict = {}
    if reqs:
        resources["requests"] = reqs
    if limits:
        resources["limits"] = limits
    return resources


def _parse_memory_bytes(raw: str) -> int:
    """Parse a k8s `resource.Quantity` memory string to an integer byte count.

    Delegates to `kubernetes.utils.parse_quantity` (the apiserver-aligned
    parser) so the supported suffix set stays in lock-step with apimachinery:
      Binary:  Ki Mi Gi Ti Pi Ei
      Decimal: k K M G T P E  (lowercase `k` is the canonical SI kilo)
    plus raw integer bytes and scientific notation (Prometheus serves large
    values as e.g. `'6.4e+10'`). The former hand-rolled suffix table silently
    returned 0 on any suffix it didn't list (bug #13) — most recently the
    lowercase `k`, which downstream comparisons read as "0 bytes" and used to
    always-trigger floor enforcement.

    `parse_quantity` returns a `Decimal`; the `int(...)` cast truncates toward
    zero, so sub-byte values (`'500m'`, `'1e-3'`) collapse to 0 exactly as the
    old fallback did. Returns 0 for empty/None-like/unparseable input so the
    comparison callers that don't wrap this in try/except get a safe sentinel
    rather than an exception.
    """
    if not raw:
        return 0
    try:
        return int(_k8s_parse_quantity(raw))
    except (ValueError, ArithmeticError):
        return 0


def _apply_grow_shrink(
    new_res: dict,
    old_res: dict | None,
    grow_only: bool,
    shrink_only: bool,
) -> dict:
    """Clamp new resources against old: grow_only keeps the max, shrink_only keeps the min."""
    if not (grow_only or shrink_only) or not old_res:
        return new_res
    result = {}
    for section in ("requests", "limits"):
        new_s = new_res.get(section, {})
        old_s = (old_res or {}).get(section, {})
        out = {}
        for key, new_val in new_s.items():
            old_val = old_s.get(key)
            if not old_val:
                out[key] = new_val
                continue
            parse = _parse_cpu_m if key == "cpu" else _parse_memory_bytes
            if grow_only and parse(new_val) < parse(old_val) or shrink_only and parse(new_val) > parse(old_val):
                out[key] = old_val
            else:
                out[key] = new_val
        if out:
            result[section] = out
    return result if result else new_res


def _enforce_floors(res: dict, b: "ResourceBounds") -> dict:
    """Re-apply min bounds after the grow/shrink guard — floors are unconditional."""
    reqs = res.get("requests", {})
    lims = res.get("limits", {})
    updated_reqs = dict(reqs)
    updated_lims = dict(lims)
    changed = False

    if b.min_cpu_request_m > 0 and "cpu" in reqs:
        if _parse_cpu_m(reqs["cpu"]) < b.min_cpu_request_m:
            updated_reqs["cpu"] = f"{b.min_cpu_request_m}m"
            changed = True
    if b.min_memory_request_mi > 0 and "memory" in reqs:
        if _parse_memory_bytes(reqs["memory"]) < b.min_memory_request_mi * (1 << 20):
            updated_reqs["memory"] = _fmt_memory(str(b.min_memory_request_mi * (1 << 20)))
            changed = True
    if b.min_cpu_limit_m > 0 and "cpu" in lims:
        if _parse_cpu_m(lims["cpu"]) < b.min_cpu_limit_m:
            updated_lims["cpu"] = f"{b.min_cpu_limit_m}m"
            changed = True
    if b.min_memory_limit_mi > 0 and "memory" in lims:
        if _parse_memory_bytes(lims["memory"]) < b.min_memory_limit_mi * (1 << 20):
            updated_lims["memory"] = _fmt_memory(str(b.min_memory_limit_mi * (1 << 20)))
            changed = True

    if not changed:
        return res
    result = dict(res)
    if reqs:
        result["requests"] = updated_reqs
    if lims:
        result["limits"] = updated_lims
        # Re-check lim >= req after floor enforcement
        r = result.get("requests", {})
        lim = result["limits"]
        if "cpu" in r and "cpu" in lim and _parse_cpu_m(lim["cpu"]) < _parse_cpu_m(r["cpu"]):
            result["limits"] = {**lim, "cpu": r["cpu"]}
        lim = result["limits"]
        if "memory" in r and "memory" in lim and _parse_memory_bytes(lim["memory"]) < _parse_memory_bytes(r["memory"]):
            result["limits"] = {**lim, "memory": r["memory"]}
    return result


def _delta_str(
    old_val: str | None,
    new_val: str,
    parse_fn,
    *,
    emoji: bool = False,
) -> str:
    """Return a delta annotation: emoji for MR, plain otherwise."""
    if not old_val:
        if emoji: return " 🆕"
        return " (new)"
    try:
        old = parse_fn(old_val)
        new = parse_fn(new_val)
        if old == 0:
            if emoji: return " 🆕"
            return " (new)"
        if old == new:
            return ""
        pct = round((new - old) / old * 100)
        sign = "+" if pct >= 0 else ""
        if emoji:
            return f" {sign}{pct}%"
        return f" ({sign}{pct}%)"
    except (ValueError, TypeError, ArithmeticError) as exc:
        _log.warning(
            "[delta-str] failed to parse resource quantity for delta annotation: "
            "old_val=%r new_val=%r — %s: %s",
            old_val, new_val, type(exc).__name__, exc,
        )
        return ""


def _fmt_delta_cpu(new_val: str, old_val: str | None, *, emoji: bool = False) -> str:
    return f"`{new_val}`{_delta_str(old_val, new_val, _parse_cpu_m, emoji=emoji)}"


def _fmt_delta_mem(new_val: str, old_val: str | None, *, emoji: bool = False) -> str:
    return f"`{new_val}`{_delta_str(old_val, new_val, _parse_memory_bytes, emoji=emoji)}"


def _query_prom_values(
    container,
    vpa: WorkloadRecommendation,
    prometheus_url: str,
    rc: "ResourceConfig",
) -> PromValues:
    """Query Prometheus for request and limit values for a single container."""
    def _cpu_req() -> int | None:
        raw = query_cpu_request_m(prometheus_url, vpa.namespace,
                                   container.container_name, vpa.target_name,
                                   rc.cpu_percentile, rc.cpu_request_window)
        return round(raw * (1 + rc.effective_cpu_request_margin)) if raw is not None else None

    def _mem_req() -> int | None:
        raw = query_mem_request_bytes(prometheus_url, vpa.namespace,
                                       container.container_name, vpa.target_name,
                                       rc.mem_percentile, rc.mem_request_window)
        return round(raw * (1 + rc.effective_mem_request_margin)) if raw is not None else None

    def _cpu_lim() -> int | None:
        raw = query_cpu_max_m(prometheus_url, vpa.namespace,
                               container.container_name, vpa.target_name, rc.cpu_limit_window)
        return round(raw * (1 + rc.effective_cpu_limit_margin)) if raw is not None else None

    def _mem_lim() -> int | None:
        raw = query_memory_max_bytes(prometheus_url, vpa.namespace,
                                      container.container_name, vpa.target_name, rc.mem_limit_window)
        return round(raw * (1 + rc.effective_mem_limit_margin)) if raw is not None else None

    return PromValues(
        cpu_request_m=_cpu_req(),
        memory_request_bytes=_mem_req(),
        cpu_limit_m=_cpu_lim(),
        memory_limit_bytes=_mem_lim(),
    )


