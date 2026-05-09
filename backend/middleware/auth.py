"""auth.py — FastAPI JWT authentication dependency.

Extracts and verifies the Supabase-issued JWT from the
``Authorization: Bearer <token>`` request header, then injects the
authenticated user's ID and email into the route context via FastAPI's
dependency injection system.

Only ES256 (asymmetric JWKS) is accepted:
  - ES256: Supabase newer projects use asymmetric keys. Public key is fetched
           from {SUPABASE_URL}/auth/v1/.well-known/jwks.json and cached in memory.
  - HS256 fallback has been removed (algorithm confusion attack surface).

Audience claim ``"authenticated"`` is enforced on every token.

All configuration is read from ``core.config`` — no credentials are
hard-coded here.

Usage in a route::

    from middleware.auth import get_current_user, AuthUser

    @router.post("/my-protected-route")
    async def my_route(auth: AuthUser = Depends(get_current_user)):
        return {"user_id": auth.user_id}

Optional / soft-auth usage (returns None when unauthenticated)::

    from middleware.auth import get_optional_user

    @router.get("/my-route")
    async def my_route(auth = Depends(get_optional_user)):
        user_id = auth.user_id if auth else None
"""

import logging
import time
from typing import Dict, Optional, Any

import jwt
import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from core.config import SUPABASE_URL, SUPABASE_JWT_SECRET  # noqa: F401

logger = logging.getLogger("uvicorn.error")

# ---------------------------------------------------------------------------
# JWKS key cache (for ES256 / asymmetric verification)
# ---------------------------------------------------------------------------

_jwks_cache: Dict[str, Any] = {}  # kid → public key object
_jwks_fetched_at: float = 0.0
_JWKS_TTL_SECONDS: int = 3600  # re-fetch JWKS every hour


def _get_jwks_key(kid: str) -> Optional[Any]:
    """Returns the public key matching ``kid`` from Supabase's JWKS endpoint.

    Results are cached in-process for ``_JWKS_TTL_SECONDS`` to avoid
    hammering the JWKS endpoint on every request.

    Args:
        kid: The ``kid`` (key ID) from the JWT header.

    Returns:
        A PyJWT-compatible public key object, or ``None`` if not found.
    """
    global _jwks_cache, _jwks_fetched_at

    now = time.monotonic()
    if not _jwks_cache or (now - _jwks_fetched_at) > _JWKS_TTL_SECONDS:
        if not SUPABASE_URL:
            logger.error("[Auth] SUPABASE_URL is not set — cannot fetch JWKS.")
            return None
        jwks_url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"
        try:
            resp = httpx.get(jwks_url, timeout=5.0)
            resp.raise_for_status()
            jwks_data = resp.json()
            new_cache: Dict[str, Any] = {}
            for key_data in jwks_data.get("keys", []):
                key_kid = key_data.get("kid", "")
                try:
                    public_key = jwt.algorithms.ECAlgorithm.from_jwk(key_data)
                    new_cache[key_kid] = public_key
                    logger.info("[Auth] Loaded JWKS public key: kid=%s", key_kid)
                except Exception as exc:
                    logger.warning("[Auth] Could not parse JWKS key kid=%s: %s", key_kid, exc)
            _jwks_cache = new_cache
            _jwks_fetched_at = now
            logger.info("[Auth] JWKS refreshed — %d key(s) loaded", len(_jwks_cache))
        except Exception as exc:
            logger.error("[Auth] Failed to fetch JWKS from %s: %s", jwks_url, exc)
            # Keep stale cache on error rather than crashing

    return _jwks_cache.get(kid)

# ---------------------------------------------------------------------------
# HTTP Bearer extractor (FastAPI built-in)
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


class AuthUser(BaseModel):
    """Authenticated user identity extracted from the Supabase JWT.

    Attributes:
        user_id: UUID of the authenticated Supabase user (``sub`` claim).
        email:   User's email address from the JWT payload.
        role:    Supabase role claim (typically ``"authenticated"``).
    """

    user_id: str
    email: str
    role: str = "authenticated"


# ---------------------------------------------------------------------------
# Core verification logic
# ---------------------------------------------------------------------------


def _decode_supabase_jwt(token: str) -> dict:
    """Decodes and verifies a Supabase-issued JWT.

    Accepts ONLY ES256 (asymmetric JWKS) tokens. HS256 is explicitly rejected
    to eliminate algorithm confusion attacks. The audience claim is enforced
    to ``"authenticated"``.

    Args:
        token: Raw JWT string (without the ``Bearer `` prefix).

    Returns:
        Decoded JWT payload dict.

    Raises:
        HTTPException 401: If the token is missing, expired, invalid,
            uses a disallowed algorithm, or fails audience validation.
        HTTPException 500: If the JWKS key cannot be resolved.
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
        alg = unverified_header.get("alg", "")

        # Accept ES256 (preferred) and HS256 (legacy/local-dev) when configured.
        if alg == "HS256":
            if not SUPABASE_JWT_SECRET:
                logger.error("[Auth] SUPABASE_JWT_SECRET is not set — cannot verify HS256 JWT.")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Server misconfigured for HS256 JWT verification.",
                )
            payload = jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience="authenticated",
                options={"verify_aud": True},
            )
            return payload

        if alg != "ES256":
            logger.warning("[Auth] Rejected token with disallowed algorithm: %s", alg)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token algorithm not permitted.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Asymmetric ES256 — resolve public key via JWKS
        kid = unverified_header.get("kid", "")
        public_key = _get_jwks_key(kid)
        if public_key is None:
            logger.error(
                "[Auth] No JWKS key found for kid=%s (alg=%s). "
                "Check SUPABASE_URL and ensure the JWKS endpoint is reachable.",
                kid, alg,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not resolve JWT signing key.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["ES256"],
            audience="authenticated",
            options={"verify_aud": True},
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except HTTPException:
        raise
    except jwt.InvalidTokenError as exc:
        try:
            unverified_header = jwt.get_unverified_header(token)
            logger.warning("[Auth] Invalid JWT: %s, header: %s", exc, unverified_header)
        except Exception:
            logger.warning("[Auth] Invalid JWT: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _extract_auth_user(payload: dict) -> AuthUser:
    """Builds an AuthUser from a decoded JWT payload.

    Args:
        payload: Decoded Supabase JWT payload dict.

    Returns:
        AuthUser instance.

    Raises:
        HTTPException 401: If the ``sub`` claim is missing.
    """
    user_id: Optional[str] = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload missing 'sub' claim.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    email: str = payload.get("email", "")
    role: str = payload.get("role", "authenticated")

    return AuthUser(user_id=user_id, email=email, role=role)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> AuthUser:
    """FastAPI dependency — requires a valid Supabase JWT.

    Injects an ``AuthUser`` into routes that mandate authentication.
    Returns HTTP 401 if the token is absent, expired, or invalid.

    Args:
        credentials: Extracted by FastAPI's HTTPBearer scheme.

    Returns:
        AuthUser: Authenticated user identity.

    Raises:
        HTTPException 401: On any auth failure.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = _decode_supabase_jwt(credentials.credentials)
    auth_user = _extract_auth_user(payload)
    logger.debug("[Auth] Authenticated user: %s", auth_user.user_id)
    return auth_user


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[AuthUser]:
    """FastAPI dependency — optional authentication.

    Returns an ``AuthUser`` when a valid token is present, otherwise
    returns ``None`` without raising an error. Use for endpoints that
    support both authenticated and anonymous access.

    Args:
        credentials: Extracted by FastAPI's HTTPBearer scheme (auto_error=False).

    Returns:
        AuthUser | None
    """
    if not credentials or not credentials.credentials:
        return None

    try:
        payload = _decode_supabase_jwt(credentials.credentials)
        return _extract_auth_user(payload)
    except HTTPException:
        return None
