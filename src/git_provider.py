"""
Git provider abstraction — GitLab (merge requests) + GitHub (pull requests).

Defines `GitProvider` as a `typing.Protocol` so that callers depend only on
the interface; `GitLabProvider` wraps the existing helpers in `src.writeback`
and is the production-proven implementation. `GitHubProvider` is fully
implemented and QA-tested via mock HTTP, selectable via config
(`gitProvider` / URL auto-detection), but not yet validated against a live
GitHub repository — treat it as alpha.

Architecture note (see docs/webhook-migration.md §write-back):
- `GitLabProvider` is constructed once per sync run in `main.cmd_sync` from
  the `Config` fields `gitlab_url`, `gitlab_token`, `gitlab_username` and
  passed down to `write_back_webhook_all` → `_commit_repo`.
- The Protocol is *structurally* typed (PEP 544 / duck-typing): third-party
  code or tests may supply any object that satisfies the five-method shape
  without subclassing `GitProvider`.

Methods
-------
auth_url(repo_url)           Build an authenticated HTTPS clone URL.
git_username()               The git Basic-auth username (credential side).
resolve_users(usernames)     Resolve git-host usernames → provider-internal
                             IDs (GitLab: numeric; GitHub: login strings).
description_cap_bytes()      Hard byte cap for PR/MR description payloads.
open_or_update_pr(...)       Idempotent open-or-adopt a pull/merge request.
"""
from __future__ import annotations

import typing
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src import log as _log_module
from src.writeback import (
    _auth_url,
    _create_gitlab_mr,
    _resolve_gitlab_user_ids,
    _safe_json,
)

_log = _log_module.get(__name__)

# The GitLab description cap (900 kB) is a platform constant, defined here
# rather than importing it from src.writeback_webhook to avoid a circular import
# (writeback_webhook imports git_provider). The value must stay in sync with
# _MR_DESCRIPTION_CAP_BYTES in writeback_webhook.py; both are intentionally
# identical literals.
_GITLAB_MR_DESCRIPTION_CAP_BYTES = 900_000

# GitHub PR body limit is 65 535 characters.  60 000 bytes is a conservative
# margin, mirroring the GitLab 900k-vs-1MiB approach (leave room for the
# truncation footer + URL encoding).
_GITHUB_PR_DESCRIPTION_CAP_BYTES = 60_000


# --------------------------------------------------------------------------- #
# GitHub HTTP session (bounded timeouts + retry)                              #
# --------------------------------------------------------------------------- #
#
# Mirrors `_GITLAB_SESSION` in src/writeback.py:
#   - retry policy: 4 total, status_forcelist {429,500,502,503,504}
#   - exponential backoff (backoff_factor=1.0)
#   - respect_retry_after_header=True
#   - raise_on_status=False so 4xx/5xx branches in open_or_update_pr fire
#
# PATCH is added to allowed_methods (GitLab uses PUT for updates;
# GitHub uses PATCH for PR description updates and issue assignments).
#
# POST is included even though RFC 7231 marks it non-idempotent: our
# open_or_update_pr is idempotent end-to-end (pre-POST adoption GET +
# 422 race-recovery), so a retried POST that the server saw lands in the
# 422 branch.
#
# Lazy singleton so QA can patch `_GITHUB_SESSION = None` to reset between
# subcases.
_GITHUB_SESSION: "requests.Session | None" = None


def _github_session() -> "requests.Session":
    """Return a lazily-initialised `requests.Session` with GitHub retry policy."""
    global _GITHUB_SESSION
    if _GITHUB_SESSION is None:
        retry = Retry(
            total=4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(("GET", "POST", "PUT", "PATCH")),
            backoff_factor=1.0,
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _GITHUB_SESSION = session
    return _GITHUB_SESSION


# Thin wrappers so tests can patch at stable symbols without poking session
# internals.  Pattern mirrors `_gitlab_get` / `_gitlab_post` / `_gitlab_put`
# in src/writeback.py.
def _github_get(url, **kwargs):
    return _github_session().get(url, **kwargs)


def _github_post(url, **kwargs):
    return _github_session().post(url, **kwargs)


def _github_patch(url, **kwargs):
    return _github_session().patch(url, **kwargs)


# --------------------------------------------------------------------------- #
# GitHub owner/repo helper                                                     #
# --------------------------------------------------------------------------- #

def _parse_github_owner_repo(project_path: str) -> tuple[str, str]:
    """Extract ``(owner, repo)`` from a GitHub project path or HTTPS URL.

    Accepts both bare paths (``"owner/repo"`` or ``"owner/repo.git"``) and
    full HTTPS URLs (``"https://github.com/owner/repo.git"``).  Strips the
    ``.git`` suffix if present.

    Raises ``ValueError`` if the path does not contain at least two non-empty
    segments — callers receive a clear error rather than a silent bad API URL.
    """
    # Strip scheme + host if a full URL was passed.
    parsed = urlparse(project_path)
    path = parsed.path if parsed.scheme else project_path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Cannot parse GitHub owner/repo from {project_path!r}: "
            "expected at least two non-empty path segments (owner/repo)"
        )
    return parts[0], parts[1]


# --------------------------------------------------------------------------- #
# Protocol                                                                     #
# --------------------------------------------------------------------------- #

@typing.runtime_checkable
class GitProvider(typing.Protocol):
    """Structural interface for a git hosting provider.

    Any object that implements all five methods below satisfies the protocol.
    Implementations are expected to be stateless with respect to individual
    sync runs — state is constructor-injected (credentials, base URL).
    """

    def auth_url(self, repo_url: str) -> str:
        """Return repo_url with credentials embedded for git over HTTPS.

        For an empty / missing token the bare repo_url is returned unchanged
        (git will fail loudly with the server's actual error, rather than
        silently skipping the push).
        """
        ...

    def has_credentials(self) -> bool:
        """Return True if a token/credential is configured for this provider.

        Used as an early-exit guard before attempting any authenticated
        operation (MR open, MR description update).  Exact equivalent of the
        old ``not gitlab_token`` check, now expressed on the provider itself
        so callers never inspect internal state.
        """
        ...

    def git_username(self) -> str:
        """Return the git Basic-auth username.

        Used as `user.name` in the git config and as the URL user component.
        Never empty — falls back to ``"oauth2"`` (GitLab's conventional
        token-auth username) when the configured value is blank.
        """
        ...

    def resolve_users(self, usernames: list[str]) -> list:
        """Resolve display/login names to provider-internal numeric IDs.

        Unknown names are logged and dropped so a typo on one reviewer never
        blocks the entire MR. Returns an empty list for empty input or when
        no token is configured.
        """
        ...

    def description_cap_bytes(self) -> int:
        """Return the byte cap for PR/MR description payloads (UTF-8 encoded).

        The provider enforces the platform's hard limit; callers pass this
        value into `_truncate_mr_description(body, cap_bytes=...)` so the
        cap can differ per provider without touching the truncation logic.
        """
        ...

    def open_or_update_pr(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        assignees: list,
        reviewers: list,
        labels: list | None,
        squash: bool,
        remove_source_branch: bool,
        project_path: str,
    ) -> str:
        """Idempotently open (or adopt an existing) pull/merge request.

        Returns the web URL of the created or pre-existing PR/MR.
        Implementations must be idempotent: calling this method a second time
        with the same (source_branch, target_branch, project_path) must return
        the same URL (updating the description), not open a duplicate.
        """
        ...


# --------------------------------------------------------------------------- #
# GitLab implementation                                                        #
# --------------------------------------------------------------------------- #

class GitLabProvider:
    """Wraps the existing GitLab helper functions behind the GitProvider interface.

    This is a thin adapter — no new logic, no new state.  All behaviour
    (retry policy, 409 race recovery, user-lookup degradation) is unchanged
    and lives in src/writeback.py.

    Parameters
    ----------
    gitlab_url : str
        Base URL of the GitLab instance, e.g. ``"https://gitlab.example.com"``.
        Must NOT have a trailing slash.
    token : str
        GitLab personal-access-token or CI job token (``GITLAB_TOKEN``).
        Empty string → git push will fail loudly; MR resolution returns [].
    username : str
        git Basic-auth username. Defaults to ``"oauth2"`` if blank, which is
        the canonical GitLab token-auth user for HTTPS clone URLs.
    """

    def __init__(self, *, gitlab_url: str, token: str, username: str = "") -> None:
        self._gitlab_url = gitlab_url
        self._token = token
        self._username = username

    # ── GitProvider protocol methods ─────────────────────────────────────── #

    def auth_url(self, repo_url: str) -> str:
        """Delegate to `_auth_url(repo_url, token, username)`.

        Special characters in the token (``@``, ``:``) are percent-encoded by
        `_auth_url` via `urllib.parse.quote` — the URL remains valid regardless
        of the token source (glpat-*, CI tokens, custom tokens).
        """
        return _auth_url(repo_url, self._token, self.git_username())

    def has_credentials(self) -> bool:
        """Return True when a non-empty token is configured.

        Exact equivalent of the old ``not gitlab_token`` check, expressed on
        the provider so callers never inspect internal state.  The guard in
        `_commit_repo` uses this instead of comparing `auth_url(...) == repo_url`
        (which is only correct for HTTPS URLs and would silently mismatch on
        SSH or custom-scheme URLs).
        """
        return bool(self._token)

    def git_username(self) -> str:
        """Return the configured username, falling back to ``"oauth2"``."""
        return self._username or "oauth2"

    def resolve_users(self, usernames: list[str]) -> list:
        """Delegate to `_resolve_gitlab_user_ids(gitlab_url, token, usernames)`.

        Returns numeric GitLab user IDs (``list[int]``).  Unknown usernames
        are logged and dropped; a completely empty input short-circuits without
        any HTTP request.
        """
        return _resolve_gitlab_user_ids(self._gitlab_url, self._token, usernames)

    def description_cap_bytes(self) -> int:
        """Return the GitLab MR description byte cap (900 000 bytes).

        GitLab's hard limit is 1 MiB; the cap is set to 900 kB to leave
        headroom for the truncation footer and URL encoding.  This matches
        the module constant `_MR_DESCRIPTION_CAP_BYTES` in writeback_webhook.
        """
        return _GITLAB_MR_DESCRIPTION_CAP_BYTES

    def open_or_update_pr(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        assignees: list,
        reviewers: list,
        labels: list | None,
        squash: bool,
        remove_source_branch: bool,
        project_path: str,
    ) -> str:
        """Delegate to `_create_gitlab_mr`.

        The function is idempotent end-to-end (pre-POST adoption GET +
        409 race recovery — see the adoption notes in writeback.py).  The ``project_path``
        parameter is accepted directly here; callers in `_commit_repo` derive
        it with `_project_path_from_url(repo_url)` before passing in.
        """
        return _create_gitlab_mr(
            gitlab_url=self._gitlab_url,
            token=self._token,
            project_path=project_path,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            description=description,
            assignee_ids=list(assignees),
            reviewer_ids=list(reviewers),
            labels=list(labels) if labels else None,
            squash=squash,
            remove_source_branch=remove_source_branch,
        )


# --------------------------------------------------------------------------- #
# GitHub implementation                                                        #
# --------------------------------------------------------------------------- #

class GitHubProvider:
    """GitHub REST API v3 implementation of the GitProvider protocol.

    Implements the five-method ``GitProvider`` Protocol so that
    ``write_back_webhook_all`` / ``_commit_repo`` can use GitHub as a target
    without any changes to the call sites.

    Status: fully implemented and QA-tested via mock HTTP; selected via the
    ``gitProvider`` config field or repo-URL auto-detection. Not yet validated
    against a live GitHub repository — treat as alpha.

    Parameters
    ----------
    token : str
        GitHub Personal Access Token or App installation token.
        Required scopes: ``repo`` (for private repos) or ``public_repo``
        (for public repos).  Empty string → ``has_credentials()`` returns
        False; authenticated operations are skipped.
    api_base_url : str
        GitHub REST API base URL.  Defaults to ``"https://api.github.com"``.
        Override for GitHub Enterprise Server (e.g.
        ``"https://github.example.com/api/v3"``).
    """

    def __init__(
        self,
        *,
        token: str,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self._token = token
        self._api_base = api_base_url.rstrip("/")

    # ── GitProvider protocol methods ─────────────────────────────────────── #

    def auth_url(self, repo_url: str) -> str:
        """Inject GitHub PAT into an HTTPS clone URL.

        GitHub convention for PAT and App installation tokens over HTTPS:
        ``https://x-access-token:<token>@host/owner/repo.git``.

        Mirrors ``_auth_url`` in src/writeback.py exactly — same
        ``.replace("https://", ...)`` + ``quote(..., safe="")`` approach so
        special characters in the token (``@``, ``:``, ``/``) are
        percent-encoded and the URL remains parseable by git.

        Empty token: returns ``repo_url`` unchanged so git fails loudly
        with the server's actual auth error rather than silently skipping.
        """
        if self._token:
            safe_user = quote("x-access-token", safe="")
            safe_token = quote(self._token, safe="")
            return repo_url.replace("https://", f"https://{safe_user}:{safe_token}@")
        return repo_url

    def has_credentials(self) -> bool:
        """Return True when a non-empty token is configured."""
        return bool(self._token)

    def git_username(self) -> str:
        """Return ``"x-access-token"`` — GitHub's conventional PAT auth username.

        GitHub accepts ``https://x-access-token:<token>@github.com/...``
        for both Personal Access Tokens and App installation tokens.
        """
        return "x-access-token"

    def resolve_users(self, usernames: list[str]) -> list:
        """Return the login strings unchanged — no HTTP call required.

        GitHub's PR API (``POST /repos/{owner}/{repo}/pulls/requested_reviewers``)
        accepts login strings directly, unlike GitLab which requires numeric
        user IDs.  An empty/falsy token returns an empty list so no reviewer
        call is made when credentials are absent.
        """
        if not self._token:
            return []
        return list(usernames)

    def description_cap_bytes(self) -> int:
        """Return the GitHub PR body byte cap (60 000 bytes).

        GitHub's hard limit is 65 535 characters; 60 000 bytes leaves
        headroom for the truncation footer and URL encoding — the same
        conservative margin used by GitLab (900 kB vs 1 MiB hard limit).
        """
        return _GITHUB_PR_DESCRIPTION_CAP_BYTES

    def open_or_update_pr(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        assignees: list,
        reviewers: list,
        labels: list | None,
        squash: bool,
        remove_source_branch: bool,
        project_path: str,
    ) -> str:
        """Idempotently open (or adopt an existing) GitHub pull request.

        Implements the same 4→5 sync-state-machine idempotency fix as
        ``_create_gitlab_mr``:

        1. Pre-POST adoption GET: ``GET /repos/{owner}/{repo}/pulls?head=...``
           If an open PR already exists for the (source, target) pair, PATCH
           its description and return its ``html_url`` without creating a
           duplicate.
        2. POST to create the PR if no existing PR was found.
        3. 422 recovery: GitHub returns 422 (not 409) when a PR already exists
           for the head+base combination.  On 422 with the "already exists"
           message, fall back to the same GET→PATCH adoption path.

        Reviewers: requested via a SEPARATE POST call after the PR exists
        (``POST .../pulls/{number}/requested_reviewers``) because GitHub does
        not accept reviewers in the PR creation payload.  A failure on the
        reviewers call is logged and swallowed — the PR URL is still returned
        so the sync run is not lost.

        Assignees: NOT implemented yet.
        GitHub assignees require a separate PATCH to the Issues API
        (``PATCH /repos/{owner}/{repo}/issues/{number}``).  The ``assignees``
        kwarg is accepted and silently ignored here; TODO comments mark the
        call sites.  The GitLab path does implement assignees;
        this is a known capability gap, not a silent drop.

        ``squash``: no-op — GitHub squash is a merge-time strategy, not a
        PR creation flag.  The kwarg is accepted and ignored.

        ``remove_source_branch``: no-op — controlled by the repo-level
        "Automatically delete head branches" setting.  The kwarg is accepted
        and ignored.

        All HTTP calls carry ``timeout=30`` (no unbounded network calls).
        On 401/403 the error message hints at required token scopes.

        Returns the ``html_url`` of the created or adopted PR.
        """
        owner, repo = _parse_github_owner_repo(project_path)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        pulls_url = f"{self._api_base}/repos/{owner}/{repo}/pulls"

        # ── Step 1: pre-POST adoption lookup (sync 4→5 idempotency fix) ──────
        # Mirrors the pre-POST adoption fix in _create_gitlab_mr.
        # If a previous sync's POST crashed mid-call after the push succeeded,
        # the work-branch is orphaned on the remote with no PR.  Re-running
        # would otherwise POST a duplicate.  Looking up any open PR for the
        # exact (source, target) pair recovers both the "POST never reached
        # server" and the "server saw POST but client timed out" cases.
        list_resp = _github_get(
            pulls_url,
            headers=headers,
            params={
                "head": f"{owner}:{source_branch}",
                "base": target_branch,
                "state": "open",
            },
            timeout=30,
        )
        self._check_auth(list_resp)
        list_resp.raise_for_status()
        existing = _safe_json(list_resp, "GitHub PR adoption lookup")
        if existing:
            pr = existing[0]
            pr_number = pr["number"]
            _log.info(
                "[github-pr] adopting existing PR #%d for branch %r",
                pr_number,
                source_branch,
            )
            patch_resp = _github_patch(
                f"{pulls_url}/{pr_number}",
                headers=headers,
                json={"title": title, "body": description},
                timeout=30,
            )
            self._check_auth(patch_resp)
            patch_resp.raise_for_status()
            self._request_reviewers(pulls_url, pr_number, reviewers, headers)
            # TODO: set assignees via PATCH /repos/{owner}/{repo}/issues/{pr_number}
            return pr["html_url"]

        # ── Step 2: POST to create ────────────────────────────────────────────
        # squash: no-op — GitHub squash is a merge-time strategy, not a PR
        #         creation flag (it is set per-merge via the merge API, or
        #         enforced as a repo-level setting).
        # remove_source_branch: no-op — GitHub controls this via the repo-level
        #         "Automatically delete head branches" setting; there is no
        #         REST equivalent at PR creation time.
        create_payload: dict = {
            "title": title,
            "body": description,
            "head": source_branch,
            "base": target_branch,
            "draft": False,
        }
        resp = _github_post(pulls_url, headers=headers, json=create_payload, timeout=30)

        # ── Step 3: 422 recovery (GitHub uses 422 for "PR already exists") ────
        # Mirrors the GitLab 409 branch: re-query for the existing PR, PATCH
        # its description, and return its url.  GitHub does NOT return 409 on
        # duplicate PR; it returns 422 Unprocessable Entity.
        if resp.status_code == 422:
            body_data = _safe_json(resp, "GitHub PR create 422 body")
            errors = body_data.get("errors") or []
            already_exists = any(
                "A pull request already exists" in (e.get("message") or "")
                for e in errors
            )
            if already_exists:
                _log.warning(
                    "[github-pr] 422 race: PR already exists for branch %r — falling back to adoption",
                    source_branch,
                )
                recovery_resp = _github_get(
                    pulls_url,
                    headers=headers,
                    params={
                        "head": f"{owner}:{source_branch}",
                        "base": target_branch,
                        "state": "open",
                    },
                    timeout=30,
                )
                self._check_auth(recovery_resp)
                recovery_resp.raise_for_status()
                prs = _safe_json(recovery_resp, "GitHub PR 422 race-recovery lookup")
                if not prs:
                    return "(PR already exists)"
                pr = prs[0]
                pr_number = pr["number"]
                patch_resp = _github_patch(
                    f"{pulls_url}/{pr_number}",
                    headers=headers,
                    json={"title": title, "body": description},
                    timeout=30,
                )
                self._check_auth(patch_resp)
                patch_resp.raise_for_status()
                self._request_reviewers(pulls_url, pr_number, reviewers, headers)
                # TODO: set assignees via issues API
                return pr["html_url"]

        self._check_auth(resp)
        resp.raise_for_status()
        pr_data = _safe_json(resp, "GitHub PR create POST response")
        pr_number = pr_data["number"]
        html_url = pr_data["html_url"]
        self._request_reviewers(pulls_url, pr_number, reviewers, headers)
        # TODO: set assignees via PATCH /repos/{owner}/{repo}/issues/{pr_number}
        return html_url

    # ── Private helpers ───────────────────────────────────────────────────── #

    def _request_reviewers(
        self,
        pulls_url: str,
        pr_number: int,
        reviewers: list[str],
        headers: dict,
    ) -> None:
        """Request reviewers on an existing PR — separate call from PR creation.

        GitHub does not accept ``reviewers`` in the PR create/update payload;
        they must be added via ``POST .../pulls/{number}/requested_reviewers``.
        This call is idempotent (re-requesting an existing reviewer is a no-op
        on GitHub's side).

        Failure is logged and swallowed — a reviewer request failure must NOT
        lose the PR URL (same non-fatal policy as GitLab reviewer degradation
        in ``_resolve_gitlab_user_ids``).
        """
        if not reviewers:
            return
        try:
            rev_resp = _github_post(
                f"{pulls_url}/{pr_number}/requested_reviewers",
                headers=headers,
                json={"reviewers": reviewers},
                timeout=30,
            )
            if rev_resp.status_code not in (200, 201):
                _log.warning(
                    "[github-pr] reviewer request for PR #%d returned status=%d — continuing",
                    pr_number,
                    rev_resp.status_code,
                )
        except Exception as exc:
            _log.warning(
                "[github-pr] reviewer request for PR #%d raised %s — continuing",
                pr_number,
                exc,
            )

    @staticmethod
    def _check_auth(resp: "requests.Response") -> None:
        """Log a helpful hint on 401/403 before raise_for_status fires.

        GitHub returns 403 when the token lacks required scopes (e.g. opening
        a PR on a private repo with a ``public_repo``-scoped token).  The
        generic HTTPError from raise_for_status does not mention scopes; this
        method logs a hint before the caller raises.
        """
        if resp.status_code in (401, 403):
            _log.error(
                "[github-pr] HTTP %d from GitHub API — check token scopes: "
                "`repo` required for private repos, `public_repo` for public. "
                "Content-Type=%r body=%r",
                resp.status_code,
                resp.headers.get("Content-Type", "?"),
                (resp.text or "")[:200],
            )


# --------------------------------------------------------------------------- #
# Provider detection + factory                                                 #
# --------------------------------------------------------------------------- #


def _detect_provider(repo_url: str) -> str:
    """Return ``"github"`` or ``"gitlab"`` by inspecting the repo URL hostname.

    Detection rules
    ---------------
    - hostname == ``"github.com"`` or ends with ``".github.com"`` → ``"github"``
    - everything else → ``"gitlab"``  (self-hosted GitLab, ``gitlab.com``,
      and any unknown host — GitHub Enterprise on a custom domain is NOT
      auto-detectable and requires an explicit ``provider_override``).

    Pure function — no I/O, no side effects.  Callers: `build_provider`.
    """
    hostname = (urlparse(repo_url).hostname or "").lower()
    if hostname == "github.com" or hostname.endswith(".github.com"):
        return "github"
    return "gitlab"


def build_provider(
    repo_url: str,
    token: str,
    *,
    provider_override: str = "",
    api_url: str = "",
    username: str = "oauth2",
) -> GitProvider:
    """Instantiate the right ``GitProvider`` implementation for ``repo_url``.

    Provider resolution
    -------------------
    - ``provider_override`` (lower-stripped) in ``("gitlab", "github")`` →
      use that provider regardless of ``repo_url``.
    - ``provider_override`` in ``("", "auto")`` → auto-detect via
      ``_detect_provider(repo_url)``.
    - Any other value → raises ``ValueError`` (config.validate pre-checks this
      so a ValueError here indicates a programming error, not user input).

    GitLab construction
    -------------------
    ``gitlab_url`` is ``api_url`` when non-empty, otherwise derived from
    ``repo_url`` via ``urlparse`` (scheme + netloc) — identical to the
    ``_effective_gitlab_url`` helper in ``main.py`` so the default self-hosted
    behaviour is byte-identical to the original GitLab-only path.

    GitHub construction
    -------------------
    ``api_base_url`` is ``api_url`` when non-empty, otherwise
    ``"https://api.github.com"`` (GitHub Enterprise requires an explicit
    ``api_url``; auto-detection cannot infer the ``/api/v3`` suffix).

    Parameters
    ----------
    repo_url : str
        The HTTPS clone URL of the writeback repo.
    token : str
        Git provider token (PAT, CI token, App installation token).
    provider_override : str
        ``"gitlab"``, ``"github"``, ``"auto"``, or ``""`` (auto).
    api_url : str
        Override for the provider API base URL.  Strip trailing slash before
        passing — the function strips it on the GitHubProvider side anyway.
    username : str
        git Basic-auth username.  Used only for GitLab; ignored for GitHub
        (always ``"x-access-token"``).

    Raises
    ------
    ValueError
        If ``provider_override`` is not in the allowed set.
    """
    override_norm = provider_override.lower().strip()
    allowed = ("", "auto", "gitlab", "github")
    if override_norm not in allowed:
        raise ValueError(
            f"build_provider: provider_override={provider_override!r} is not "
            f"in the allowed set {allowed!r}. Set gitProvider to one of "
            f"'gitlab', 'github', 'auto', or leave empty for auto-detect."
        )

    resolved = override_norm if override_norm in ("gitlab", "github") else _detect_provider(repo_url)

    if resolved == "github":
        return GitHubProvider(
            token=token,
            api_base_url=(api_url or "https://api.github.com"),
        )

    # GitLab path: derive base URL from repo_url when api_url is empty,
    # matching _effective_gitlab_url in main.py exactly.
    if api_url:
        gitlab_url = api_url
    else:
        parsed = urlparse(repo_url)
        gitlab_url = f"{parsed.scheme}://{parsed.netloc}"

    return GitLabProvider(
        gitlab_url=gitlab_url,
        token=token,
        username=username,
    )
