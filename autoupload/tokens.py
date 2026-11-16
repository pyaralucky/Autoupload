"""Renew the long-lived Instagram token and store it back as a GitHub secret."""

from __future__ import annotations

import base64

import requests

from .config import ConfigError
from .instagram import DEFAULT_API_BASE, DEFAULT_API_VERSION, InstagramError
from .notify import GITHUB_API


def refresh_access_token(env: dict, session) -> tuple[str, int | None]:
    """Returns (new token, seconds until it expires or None if unknown)."""
    token = env.get("IG_ACCESS_TOKEN")
    if not token:
        raise ConfigError("Missing required setting: IG_ACCESS_TOKEN")
    base = (env.get("IG_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    if "graph.instagram.com" in base:
        url = f"{base}/refresh_access_token"
        params = {"grant_type": "ig_refresh_token", "access_token": token}
    else:
        app_id, secret = env.get("FB_APP_ID"), env.get("FB_APP_SECRET")
        if not (app_id and secret):
            raise ConfigError(
                "Refreshing a Facebook Login token needs FB_APP_ID and FB_APP_SECRET"
            )
        version = env.get("IG_API_VERSION") or DEFAULT_API_VERSION
        url = f"{base}/{version}/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": secret,
            "fb_exchange_token": token,
        }
    try:
        resp = session.request("GET", url, params=params, timeout=60)
        body = resp.json()
    except requests.RequestException as exc:
        # The request URL carries the token; report the error type only.
        raise InstagramError(f"token refresh: network error ({type(exc).__name__})") from None
    except ValueError:
        body = {}
    if resp.status_code >= 400 or "access_token" not in body:
        err = body.get("error") if isinstance(body, dict) else None
        detail = err.get("message") if isinstance(err, dict) else None
        raise InstagramError(
            f"token refresh failed (HTTP {resp.status_code}): {detail or 'no detail'}"
        )
    return body["access_token"], body.get("expires_in")


def set_github_secret(env: dict, session, name: str, value: str) -> None:
    """Encrypt ``value`` for the repository and store it as an Actions secret."""
    from nacl import encoding, public

    token, repo = env.get("GH_SECRETS_TOKEN"), env.get("GITHUB_REPOSITORY")
    if not (token and repo):
        raise ConfigError(
            "Saving the refreshed token needs GH_SECRETS_TOKEN (a token with "
            "permission to write repository secrets) and GITHUB_REPOSITORY"
        )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    base = f"{GITHUB_API}/repos/{repo}/actions/secrets"
    try:
        key_resp = session.request("GET", f"{base}/public-key", headers=headers, timeout=30)
        key_resp.raise_for_status()
        key = key_resp.json()
        sealed = public.SealedBox(
            public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
        ).encrypt(value.encode())
        put = session.request(
            "PUT",
            f"{base}/{name}",
            headers=headers,
            json={
                "encrypted_value": base64.b64encode(sealed).decode(),
                "key_id": key["key_id"],
            },
            timeout=30,
        )
        put.raise_for_status()
    except requests.RequestException as exc:
        raise InstagramError(f"could not update the {name} secret: {exc}") from exc
