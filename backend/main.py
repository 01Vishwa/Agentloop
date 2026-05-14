"""FastAPI application entry point.

Mounts the API router, registers middleware, and applies CORS. All
cross-cutting concerns (error handling) are imported from dedicated modules.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

from api.routes import router as api_router
from core.config import (
    SUPABASE_URL,
    SUPABASE_PUBLISHABLE_KEY,
    SUPABASE_JWT_SECRET,
    SUPABASE_SERVICE_ROLE_KEY,
    NVIDIA_API_KEY,
)
from middleware.error_handler import global_exception_handler
from middleware.request_logger import RequestLoggerMiddleware


app = FastAPI(
    title="Agentloop Backend",
    description="Intelligent Document Processing API",
    version="1.0.0"
)

_allowed_origins_raw = os.getenv("ALLOWED_ORIGINS", "")
if not _allowed_origins_raw.strip():
    raise ValueError(
        "ALLOWED_ORIGINS environment variable is not set. "
        "Set it to a comma-separated list of allowed frontend origins "
        "(e.g. ALLOWED_ORIGINS=https://app.example.com) before starting the server."
    )
allowed_origins = [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Structured per-request logging (method, path, user, status, latency)
app.add_middleware(RequestLoggerMiddleware)

app.add_exception_handler(Exception, global_exception_handler)

from api.routes import _evict_stale_sessions as _evict_sessions


@app.on_event("startup")
async def _start_session_eviction_loop() -> None:
    """B2 fix: Periodically evicts stale sessions every 60 s.

    @router.on_event(\"startup\") silently no-ops on APIRouter; registering
    the task here on the FastAPI app instance ensures it actually runs.
    """
    import asyncio as _asyncio
    import logging as _log_mod
    _l = _log_mod.getLogger("uvicorn.error")

    async def _loop() -> None:
        while True:
            await _asyncio.sleep(60)
            try:
                _evict_sessions()
            except Exception as exc:  # pylint: disable=broad-except
                _l.error("[Router] Eviction loop error: %s", exc)

    _asyncio.create_task(_loop())


@app.on_event("startup")
async def startup_event():
    """Validates configuration on startup and prints config health table."""
    import logging as _logging
    _log = _logging.getLogger("uvicorn.error")

    checks = {
        "SUPABASE_URL":              bool(SUPABASE_URL),
        "SUPABASE_PUBLISHABLE_KEY":  bool(SUPABASE_PUBLISHABLE_KEY),
        "SUPABASE_JWT_SECRET":       bool(SUPABASE_JWT_SECRET),
        "SUPABASE_SERVICE_ROLE_KEY": bool(SUPABASE_SERVICE_ROLE_KEY)
                                     and SUPABASE_SERVICE_ROLE_KEY.startswith("eyJ"),
        "NVIDIA_API_KEY":            bool(NVIDIA_API_KEY),
    }

    _log.info("=" * 60)
    _log.info("Agentloop config health:")
    for name, ok in checks.items():
        status = "OK  ✓" if ok else "MISSING / INVALID  ✗"
        _log.info("  %-36s %s", name, status)
    _log.info("=" * 60)

    missing = [k for k, v in checks.items() if not v]
    if "SUPABASE_SERVICE_ROLE_KEY" in missing:
        _log.error(
            "SUPABASE_SERVICE_ROLE_KEY is missing or wrong format. "
            "It must start with 'eyJ' (JWT). "
            "Get it from: Supabase Dashboard → Settings → API → service_role"
        )
    other_missing = [k for k in missing if k != "SUPABASE_SERVICE_ROLE_KEY"]
    if other_missing:
        _log.error(
            "Missing environment variables: %s. Check backend/.env.",
            ", ".join(other_missing),
        )

app.include_router(api_router, prefix="/api")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
