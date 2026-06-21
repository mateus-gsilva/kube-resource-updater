"""GitHub App authentication — mint a short-lived installation access token.

Used by ``build_provider`` when ``git.appId`` / ``git.installationId`` and an
App private key are configured instead of a PAT. The flow:

  1. Sign a 9-minute App JWT (RS256) with the App private key.
  2. Exchange it for an installation access token (~1h TTL) via
     ``POST /app/installations/{id}/access_tokens``.

The token is then used exactly like a PAT (``x-access-token:<token>@host`` for
git over HTTPS and ``Authorization: Bearer`` for the Pulls API).

Crypto note: signing uses ``cryptography`` (already a dependency for the
in-process webhook cert reconciler) — no PyJWT. JWT assembly is stdlib
``base64`` / ``json``.

Lifetime note: the token is minted ONCE per sync at provider construction and
not refreshed mid-run. The CronJob's default ``activeDeadlineSeconds`` (1800s)
is comfortably under the installation-token TTL (3600s). Installs that raise
``cronjob.activeDeadlineSeconds`` above ~3300s should be aware a very long
sync's final push could outlive the token.
"""
from __future__ import annotations

import base64
import json
import logging
import time

_log = logging.getLogger("kube-resource-updater")

# GitHub rejects an App JWT whose ``exp`` is more than 10 minutes out. 9 minutes
# leaves headroom for the 60s ``iat`` backdate below.
_APP_JWT_TTL_S = 540


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding (JWT segment encoding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_app_jwt(app_id: str, private_key_pem: str, now: int | None = None) -> str:
    """Return a signed RS256 GitHub App JWT.

    ``iat`` is backdated 60s to tolerate a local clock up to 60s ahead of
    GitHub's (GitHub rejects a JWT whose ``iat`` is in the future). ``exp`` is
    ``now + 540s``, under GitHub's 10-minute cap.

    Raises ``ValueError`` with an operator-facing message when the PEM cannot be
    loaded — the most common cause is a key pasted with escaped ``\\n`` instead
    of real newlines, which this also tries to repair before failing.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    if now is None:
        now = int(time.time())

    # Operators frequently paste the PEM with escaped "\n" (it survives a
    # round-trip through some Secret tooling and copy/paste). Normalize so
    # load_pem_private_key doesn't fail with an opaque deserialization error.
    pem = private_key_pem.replace("\\n", "\n")
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "GitHub App private key could not be loaded — ensure the PEM is "
            "stored with real newlines (not escaped \\n) and is an RSA key. "
            f"Underlying error: {exc}"
        ) from exc

    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + _APP_JWT_TTL_S, "iss": str(app_id)}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    signature = key.sign(
        signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
    )
    return f"{signing_input}.{_b64url(signature)}"


def mint_installation_token(
    app_id: str,
    installation_id: str,
    private_key_pem: str,
    *,
    api_base: str = "https://api.github.com",
) -> str:
    """Exchange an App JWT for a short-lived installation access token.

    ``api_base`` is the GitHub REST API base — ``https://api.github.com`` for
    github.com, or the GHES equivalent (``https://ghe.example/api/v3``).

    Raises ``RuntimeError`` on any non-2xx response, a non-JSON body, or a body
    without a ``token`` field. The message points at the most common operator
    error (the App not being installed on the target repo).
    """
    # Lazy import avoids an import cycle: git_provider imports this module
    # lazily inside build_provider, so importing git_provider at call time here
    # is safe (it is fully loaded by then) and lets tests patch
    # ``src.git_provider._github_post`` at a stable symbol.
    import src.git_provider as _gp

    jwt = build_app_jwt(app_id, private_key_pem)
    url = f"{api_base.rstrip('/')}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = _gp._github_post(url, headers=headers, timeout=30)

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"GitHub App token exchange failed: POST {url} returned "
            f"{resp.status_code}. Confirm App {app_id!r} is installed on the "
            f"target repo and that installation id {installation_id!r} is correct."
        )
    try:
        body = resp.json()
    except ValueError as exc:  # json.JSONDecodeError subclasses ValueError
        raise RuntimeError(
            f"GitHub App token exchange returned a non-JSON body from {url}: {exc}"
        ) from exc

    token = (body or {}).get("token", "")
    if not token:
        raise RuntimeError(
            f"GitHub App token exchange response from {url} contained no 'token' field"
        )

    _log.info(
        "[github-app] minted installation token (app_id=%s installation_id=%s)",
        app_id, installation_id,
    )
    return token
