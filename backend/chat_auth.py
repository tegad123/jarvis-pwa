"""
Chat-mode auth: single-password gate with HMAC-signed session cookies.

Cookie value format:  "<issued_unix_ts>.<hmac_sha256_hex>"
Signature key:        JARVIS_PWA_SESSION_SECRET (env)
Compared password:    JARVIS_PWA_PASSWORD (env)

TODO(security): no rate limiting on /chat/api/login. This is a single-user
local PWA on Spencer's phone behind a Cloudflare tunnel, so brute-force
isn't a realistic threat today. Revisit before exposing publicly or adding
multi-user support. See README "Known Limitations".
"""

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

from fastapi import HTTPException, Request, Response

log = logging.getLogger("jarvis-pwa.chat-auth")

COOKIE_NAME = "jarvis_chat_session"
COOKIE_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
COOKIE_PATH = "/"  # whole app: Talk/Record/Memos/Chat all share one session


def _secret() -> bytes:
    s = os.getenv("JARVIS_PWA_SESSION_SECRET", "")
    if not s:
        raise RuntimeError("JARVIS_PWA_SESSION_SECRET is not set")
    return s.encode()


def _password() -> str:
    p = os.getenv("JARVIS_PWA_PASSWORD", "")
    if not p:
        raise RuntimeError("JARVIS_PWA_PASSWORD is not set")
    return p


def _sign(ts: int) -> str:
    return hmac.new(_secret(), str(ts).encode(), hashlib.sha256).hexdigest()


def _mint(ts: Optional[int] = None) -> str:
    ts = ts if ts is not None else int(time.time())
    return f"{ts}.{_sign(ts)}"


def verify_password(submitted: str) -> bool:
    return hmac.compare_digest(submitted or "", _password())


def is_authenticated(request: Request) -> bool:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw or "." not in raw:
        return False
    ts_str, sig = raw.split(".", 1)
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    expected = _sign(ts)
    if not hmac.compare_digest(sig, expected):
        return False
    if int(time.time()) - ts > COOKIE_TTL_SECONDS:
        return False
    return True


def issue_session_cookie(response: Response) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=_mint(),
        max_age=COOKIE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path=COOKIE_PATH,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path=COOKIE_PATH)


def require_auth(request: Request) -> None:
    """FastAPI dependency: 401 if no valid session cookie."""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="not authenticated")
