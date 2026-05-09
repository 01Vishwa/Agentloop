"""request_logger.py — Starlette middleware for structured per-request logging.

Logs every HTTP request and its response with:
  - Method, path, query string
  - Authenticated user_id (decoded from Bearer token without re-verifying,
    since auth middleware already runs before route handlers)
  - HTTP response status code
  - Wall-clock latency in milliseconds

Intentionally lightweight: no body buffering, no PII in the log.

Usage (register in main.py):
    from middleware.request_logger import RequestLoggerMiddleware
    app.add_middleware(RequestLoggerMiddleware)
"""

import logging
import time
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

import jwt as pyjwt

logger = logging.getLogger("uvicorn.info")

# Paths to skip (health checks, static assets, etc.)
_SKIP_PATHS = frozenset({"/docs", "/openapi.json", "/redoc", "/favicon.ico"})


def _extract_user_id(request: Request) -> Optional[str]:
    """Best-effort extraction of user_id from the Bearer token.

    Does NOT verify the signature — auth middleware already did that.
    Returns None on any failure so we never block a request here.
    """
    try:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header.removeprefix("Bearer ").strip()
        # Decode without verification — we only want the sub claim for logging
        payload = pyjwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["ES256", "HS256"],
        )
        return payload.get("sub", "unknown")[:8]  # first 8 chars for brevity
    except Exception:  # pylint: disable=broad-except
        return None


class RequestLoggerMiddleware(BaseHTTPMiddleware):
    """Logs each request as two lines: one before dispatch, one after.

    Example output:
        INFO: [→] POST /api/upload        user=1ea5c7ac  files=pokemon.csv
        INFO: [←] POST /api/upload        200 OK  42ms
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Skip noisy static / doc paths
        if path in _SKIP_PATHS or path.startswith("/static"):
            return await call_next(request)

        method = request.method
        query = str(request.url.query)
        user_id = _extract_user_id(request)
        user_tag = f"user={user_id}" if user_id else "user=anon"
        query_tag = f"?{query}" if query else ""

        logger.info("[→] %s %s%s  %s", method, path, query_tag, user_tag)

        t0 = time.monotonic()
        try:
            response: Response = await call_next(request)
        except Exception as exc:  # pylint: disable=broad-except
            elapsed = int((time.monotonic() - t0) * 1000)
            logger.error("[←] %s %s  ERROR %s  %dms", method, path, exc, elapsed)
            raise

        elapsed = int((time.monotonic() - t0) * 1000)
        status_code = response.status_code
        level = logging.WARNING if status_code >= 400 else logging.INFO
        logger.log(
            level,
            "[←] %s %s  %d  %dms",
            method,
            path,
            status_code,
            elapsed,
        )
        return response
