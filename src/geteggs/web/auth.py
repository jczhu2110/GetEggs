"""Localhost security guards: startup token exchange, HttpOnly cookie, and Origin checks."""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status

COOKIE_NAME = "geteggs_session"
CSRF_HEADER_NAME = "x-geteggs-request"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SessionAuthManager:
    """Manages startup token exchange and signed local session cookie."""

    def __init__(self, *, enabled: bool = True, startup_token: str | None = None) -> None:
        self.enabled = enabled
        self.startup_token = startup_token or secrets.token_urlsafe(24)
        self.session_value = secrets.token_urlsafe(32)

    def verify_startup_token(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not token:
            return False
        return hmac.compare_digest(token, self.startup_token)

    def verify_request(self, request: Request) -> None:
        if not self.enabled:
            return
        cookie_val = request.cookies.get(COOKIE_NAME)
        if not cookie_val or not hmac.compare_digest(cookie_val, self.session_value):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="未授权的本地会话，请通过控制台启动链接重新打开页面。",
            )


def _parse_host_header(raw_host: str) -> tuple[str, int | None] | None:
    cleaned = (raw_host or "").strip()
    if not cleaned or any(ch in cleaned for ch in (" ", "\t", "/", "\\", "@", "?", "#")):
        return None
    try:
        parsed = urlparse(f"http://{cleaned}")
        port = parsed.port
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    return parsed.hostname.lower(), port


def _effective_port(scheme: str, port: int | None) -> int:
    if port is not None:
        return port
    return 443 if scheme == "https" else 80


def _url_matches_local_origin(
    url_value: str,
    allowed_hosts: set[str],
    expected_port: int | None,
    expected_scheme: str,
) -> bool:
    try:
        parsed = urlparse(url_value)
        port = parsed.port
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or host not in allowed_hosts:
        return False
    if _effective_port(scheme, port) != _effective_port(expected_scheme, expected_port):
        return False
    return True


def is_local_origin(request: Request) -> bool:
    """Reject cross-origin and cross-port requests to block browser CSRF."""
    allowed_hosts = set(LOCAL_HOSTS)
    # Allow Starlette's in-process ASGI TestClient without exposing `testserver` over TCP.
    if request.client is not None and request.client.host == "testclient":
        allowed_hosts.add("testserver")

    parsed_host = _parse_host_header(request.headers.get("host") or "")
    if parsed_host is None:
        return False
    host_name, host_port = parsed_host
    if host_name not in allowed_hosts:
        return False
    request_scheme = (request.url.scheme or "http").lower()

    sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    sec_fetch_mode = (request.headers.get("sec-fetch-mode") or "").strip().lower()
    if sec_fetch_site and sec_fetch_site not in ("same-origin", "none"):
        # Allow top-level navigation from the local launcher splash page (different loopback port)
        # to /auth and its immediate redirect to /; API and subresource requests remain same-origin only.
        is_splash_auth_nav = (
            request.method.upper() == "GET"
            and request.url.path in ("/", "/auth")
            and sec_fetch_site == "same-site"
            and sec_fetch_mode == "navigate"
        )
        if not is_splash_auth_nav:
            return False

    origin = request.headers.get("origin")
    if origin:
        if not _url_matches_local_origin(origin, allowed_hosts, host_port, request_scheme):
            return False

    referer = request.headers.get("referer")
    if referer and request.method.upper() not in _SAFE_METHODS:
        if not _url_matches_local_origin(referer, allowed_hosts, host_port, request_scheme):
            return False

    # Browser state-changing requests carry Origin or Sec-Fetch-Mode; require a
    # custom non-simple header so bodyless cross-port POSTs cannot bypass CORS.
    if (
        request.method.upper() not in _SAFE_METHODS
        and (origin or request.headers.get("sec-fetch-mode"))
        and request.headers.get(CSRF_HEADER_NAME) != "1"
    ):
        return False

    return True
