"""One-time sign-in that produces the YouTube refresh token.

Run ``python -m autoupload youtube-auth`` on your own computer. It opens
Google's consent page, receives the answer on a local port and prints the
token to store as the ``YT_REFRESH_TOKEN`` secret.
"""

from __future__ import annotations

import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from .config import ConfigError
from .youtube import AUTH_URL, SCOPES, TOKEN_URL, YouTubeError


def consent_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",  # ...even if this account approved the app before
        "state": state,
    })


def exchange(session, client_id: str, client_secret: str, code: str, redirect_uri: str) -> str:
    try:
        resp = session.request(
            "POST",
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            timeout=60,
        )
        body = resp.json()
    except requests.RequestException as exc:
        raise YouTubeError(f"Google sign-in: network error ({type(exc).__name__})") from None
    except ValueError:
        body = {}
    if resp.status_code >= 400 or not body.get("refresh_token"):
        detail = body.get("error_description") or body.get("error") or "no refresh token returned"
        raise YouTubeError(f"Google sign-in failed (HTTP {resp.status_code}): {detail}")
    return body["refresh_token"]


def wait_for_code(server: HTTPServer, state: str) -> str:
    """Serve requests until Google redirects back with the authorisation code."""
    answer: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server naming)
            query = parse_qs(urlsplit(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_error(400, "Unexpected request")
                return
            answer["code"] = query.get("code", [""])[0]
            answer["error"] = query.get("error", [""])[0]
            text = "Signed in. You can close this tab." if answer["code"] else "Sign-in was cancelled."
            body = f"<html><body><p>{text}</p></body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the code out of the terminal
            pass

    server.RequestHandlerClass = Handler
    while "code" not in answer:
        server.handle_request()
    if not answer["code"]:
        raise YouTubeError(f"Google sign-in was not completed ({answer['error'] or 'no code'})")
    return answer["code"]


def sign_in(env: dict, session, port: int = 0, open_browser: bool = True) -> str:
    client_id, client_secret = env.get("YT_CLIENT_ID"), env.get("YT_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise ConfigError(
            "Set YT_CLIENT_ID and YT_CLIENT_SECRET (in .env) to the OAuth client "
            "of type 'Desktop app' from the Google Cloud console."
        )
    server = HTTPServer(("127.0.0.1", port), lambda *a: None)
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
        state = secrets.token_urlsafe(16)
        url = consent_url(client_id, redirect_uri, state)
        print("Open this address and sign in with the Google account that owns the channel:\n")
        print(url + "\n")
        if open_browser:
            webbrowser.open(url)
        code = wait_for_code(server, state)
    finally:
        server.server_close()
    return exchange(session, client_id, client_secret, code, redirect_uri)
