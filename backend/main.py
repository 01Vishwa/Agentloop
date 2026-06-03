"""FastAPI application entry point.

Mounts the API router, registers middleware, and applies CORS. All
cross-cutting concerns (error handling) are imported from dedicated modules.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

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
from api.routes import _evict_stale_sessions as _evict_sessions

_logger = logging.getLogger("uvicorn.error")

# Handle for the background eviction task — kept module-level so it can be
# inspected by health checks or tests without maintaining additional state.
_eviction_task_handle: "asyncio.Task | None" = None


# ---------------------------------------------------------------------------
# Lifespan — replaces deprecated @app.on_event("startup") / ("shutdown")
# ---------------------------------------------------------------------------


async def _eviction_loop() -> None:
    """Inner loop: runs forever, catching per-tick errors without stopping."""
    while True:
        await asyncio.sleep(60)
        try:
            _evict_sessions()
        except Exception as exc:  # pylint: disable=broad-except
            _logger.error(
                "[Eviction] Tick error (loop continues): %s", exc, exc_info=True
            )


async def _supervised_eviction() -> None:
    """Outer supervisor: restarts the eviction loop on unexpected exit.

    BUG 2 fix: Supervised background task that evicts stale sessions.

    The original implementation used a bare ``asyncio.create_task`` whose loop
    would die silently on any unhandled exception, causing memory to grow
    unbounded.  This version:

    * Wraps the eviction call in a per-iteration try/except that logs
      errors but never breaks the loop.
    * Wraps the *entire* loop body in a supervisor that restarts the inner
      task automatically after an exponentially backed-off delay (2s → 64s cap)
      so a transient bug can't permanently disable eviction.
    """
    _backoff = 2  # seconds — doubles on each crash, capped at 64 s
    while True:
        try:
            _logger.info("[Eviction] Starting session-eviction loop.")
            await _eviction_loop()
        except asyncio.CancelledError:
            _logger.info("[Eviction] Loop cancelled — shutting down cleanly.")
            return  # propagate cancellation — do not restart
        except Exception as exc:  # pylint: disable=broad-except
            _logger.error(
                "[Eviction] Loop crashed (%s). Restarting in %ds.",
                exc,
                _backoff,
                exc_info=True,
            )
            await asyncio.sleep(_backoff)
            _backoff = min(_backoff * 2, 64)  # exponential back-off, cap 64 s
        else:
            # _eviction_loop exited cleanly (shouldn't happen — it loops forever)
            _logger.warning(
                "[Eviction] Loop exited unexpectedly. Restarting in %ds.", _backoff
            )
            await asyncio.sleep(_backoff)
            _backoff = min(_backoff * 2, 64)


def _log_config_health() -> None:
    """Validates configuration on startup and prints config health table."""
    checks = {
        "SUPABASE_URL":              bool(SUPABASE_URL),
        "SUPABASE_PUBLISHABLE_KEY":  bool(SUPABASE_PUBLISHABLE_KEY),
        "SUPABASE_JWT_SECRET":       bool(SUPABASE_JWT_SECRET),
        "SUPABASE_SERVICE_ROLE_KEY": bool(SUPABASE_SERVICE_ROLE_KEY)
                                     and SUPABASE_SERVICE_ROLE_KEY.startswith("eyJ"),
        "NVIDIA_API_KEY":            bool(NVIDIA_API_KEY),
    }

    _logger.info("=" * 60)
    _logger.info("Agentloop config health:")
    for name, ok in checks.items():
        label = "OK  ✓" if ok else "MISSING / INVALID  ✗"
        _logger.info("  %-36s %s", name, label)
    _logger.info("=" * 60)

    missing = [k for k, v in checks.items() if not v]
    if "SUPABASE_SERVICE_ROLE_KEY" in missing:
        _logger.error(
            "SUPABASE_SERVICE_ROLE_KEY is missing or wrong format. "
            "It must start with 'eyJ' (JWT). "
            "Get it from: Supabase Dashboard → Settings → API → service_role"
        )
    other_missing = [k for k in missing if k != "SUPABASE_SERVICE_ROLE_KEY"]
    if other_missing:
        _logger.error(
            "Missing environment variables: %s. Check backend/.env.",
            ", ".join(other_missing),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown logic.

    Replaces the deprecated ``@app.on_event("startup")`` pattern.
    Everything before ``yield`` runs on startup; everything after runs on
    shutdown.
    """
    global _eviction_task_handle  # noqa: PLW0603

    # --- Startup ---
    _log_config_health()
    _eviction_task_handle = asyncio.create_task(_supervised_eviction())

    yield

    # --- Shutdown ---
    if _eviction_task_handle and not _eviction_task_handle.done():
        _eviction_task_handle.cancel()
        try:
            await _eviction_task_handle
        except asyncio.CancelledError:
            pass
        _logger.info("[Eviction] Background task stopped.")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

_allowed_origins_raw = os.getenv("ALLOWED_ORIGINS", "")
if not _allowed_origins_raw.strip():
    raise ValueError(
        "ALLOWED_ORIGINS environment variable is not set. "
        "Set it to a comma-separated list of allowed frontend origins "
        "(e.g. ALLOWED_ORIGINS=https://app.example.com) before starting the server."
    )
allowed_origins = [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()]

app = FastAPI(
    title="Agentloop Backend",
    description="Intelligent Document Processing API",
    version="1.0.0",
    lifespan=lifespan,
)

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

app.include_router(api_router, prefix="/api")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
