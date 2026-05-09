"""Eval Dashboard API — read-only endpoints served under /api/eval/*.

All routes are GET except one POST for triggering offline eval (deferred).
No authentication required — same open policy as the rest of the API.

Endpoints:
    GET  /api/eval/overview           — system-level KPIs
    GET  /api/eval/agents             — per-agent latency + failure rates
    GET  /api/eval/debug-loop         — debug loop depth, error distribution
    GET  /api/eval/runs               — paginated run list with metrics
    GET  /api/eval/runs/{run_id}/trace — step-by-step trace for one run
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from middleware.auth import AuthUser, get_current_user

logger = logging.getLogger("uvicorn.info")

eval_router = APIRouter(tags=["eval"])


# ---------------------------------------------------------------------------
# System Overview
# ---------------------------------------------------------------------------

@eval_router.get("/overview")
async def eval_overview(
    auth: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Returns aggregated system-level KPIs from all recorded runs.

    Returns:
        Dict with ``total_runs``, ``success_rate`` (%),
        ``avg_latency_ms``, ``avg_retries``, ``easy_count``, ``hard_count``.
    """
    try:
        from eval.eval_store import get_overview_stats  # pylint: disable=import-outside-toplevel
        return await get_overview_stats()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("[EvalRoutes] overview error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Agent Performance
# ---------------------------------------------------------------------------

@eval_router.get("/agents")
async def eval_agents(
    auth: AuthUser = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Returns per-agent performance metrics aggregated across all runs.

    Returns:
        List of ``{agent_name, avg_latency_ms, failure_rate, total_calls}``.
    """
    try:
        from eval.eval_store import get_agent_stats  # pylint: disable=import-outside-toplevel
        return await get_agent_stats()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("[EvalRoutes] agents error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Debug Loop Analysis
# ---------------------------------------------------------------------------

@eval_router.get("/debug-loop")
async def eval_debug_loop(
    auth: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Returns debug loop depth and error type distribution.

    Returns:
        Dict with ``avg_debug_depth``, ``avg_retries``,
        ``retry_success_ratio``, ``error_type_distribution``.
    """
    try:
        from eval.eval_store import get_debug_loop_stats  # pylint: disable=import-outside-toplevel
        return await get_debug_loop_stats()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("[EvalRoutes] debug-loop error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Run List
# ---------------------------------------------------------------------------

@eval_router.get("/runs")
async def eval_runs(
    limit: int = Query(default=50, ge=1, le=100),
    difficulty: Optional[str] = Query(default=None, pattern="^(easy|hard)$"),
    mode: Optional[str] = Query(default=None, pattern="^(live|batch)$"),
    auth: AuthUser = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Returns a paginated list of runs with their computed eval metrics.

    Query params:
        limit:      Max rows (1–100, default 50).
        difficulty: Filter by task difficulty (``easy`` | ``hard``).
        mode:       Filter by eval mode (``live`` | ``batch``).

    Returns:
        List of eval_metrics rows joined with agent_runs metadata.
    """
    try:
        from eval.eval_store import list_run_metrics  # pylint: disable=import-outside-toplevel
        return await list_run_metrics(limit=limit, difficulty=difficulty, mode=mode)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("[EvalRoutes] runs error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Run Trace
# ---------------------------------------------------------------------------

@eval_router.get("/runs/{run_id}/trace")
async def eval_run_trace(
    run_id: str,
    auth: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Returns the step-by-step eval trace and metadata for one run.

    Args:
        run_id: The agent_runs.id UUID.

    Returns:
        Dict with ``run_id``, ``steps`` (list), and ``agent_runs`` metadata.
    """
    try:
        from eval.eval_store import list_steps_for_run  # pylint: disable=import-outside-toplevel
        from services.supabase_service import get_agent_run  # pylint: disable=import-outside-toplevel

        steps, run = await _gather(
            list_steps_for_run(run_id),
            get_agent_run(run_id),
        )

        if not run:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found in agent_runs.",
            )

        return {"run_id": run_id, "run": run, "steps": steps}
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("[EvalRoutes] trace error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402  (import after main defs is intentional)


async def _gather(*coros):
    return await asyncio.gather(*coros)
