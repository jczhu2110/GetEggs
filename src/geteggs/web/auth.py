"""Localhost security guards: startup token exchange, HttpOnly cookie, and Origin checks."""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status

COOKIE_NAME = "geteggs_session"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}


class SessionAuthManager:
    """Manages one-time startup token and signed local session cookie."""

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


def is_local_origin(request: Request) -> bool:
    """Reject cross-origin requests from non-localhost origins to block browser CSRF."""
    host_header = (request.headers.get("host") or "").split(":")[0].lower()
    if host_header and host_header not in LOCAL_HOSTS:
        return False

    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if (parsed.hostname or "").lower() not in LOCAL_HOSTS:
            return False

    referer = request.headers.get("referer")
    if referer and request.method not in ("GET", "HEAD", "OPTIONS"):
        parsed = urlparse(referer)
        if (parsed.hostname or "").lower() not in LOCAL_HOSTS:
            return False

    return True
