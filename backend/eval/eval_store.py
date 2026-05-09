"""EvalStore — Supabase persistence for the evaluation sidecar.

Mirrors the async executor pattern from services/supabase_service.py:
all blocking supabase-py calls are dispatched via run_in_executor so the
FastAPI event loop is never blocked during SSE streaming.
"""

import asyncio
import logging
from typing import Any, Dict, List

from eval.schemas import EvalRunMetrics, EvalStep

logger = logging.getLogger("uvicorn.info")


# ---------------------------------------------------------------------------
# Internal helper — reuse the same Supabase client from services
# ---------------------------------------------------------------------------

def _get_client(service_role: bool = False):
    from services.supabase_service import get_supabase_client, get_service_role_client  # pylint: disable=import-outside-toplevel
    if service_role:
        return get_service_role_client()
    return get_supabase_client()


def _is_auth_failed() -> bool:
    """Returns the circuit-breaker flag from supabase_service."""
    try:
        from services import supabase_service  # pylint: disable=import-outside-toplevel
        return supabase_service._supabase_auth_failed  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-except
        return False


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

async def upsert_steps(steps: List[EvalStep]) -> None:
    """Batch-inserts EvalStep rows into the ``eval_steps`` table.

    Uses upsert (on_conflict=id) so re-runs of finalize() are safe.

    Args:
        steps: Collected EvalStep objects for the run.
    """
    if not steps:
        return

    records = [
        {
            "id":                s.step_id,
            "run_id":            s.run_id,
            "step_seq":          s.step_seq,
            "round_num":         s.round_num,
            "agent_name":        s.agent_name,
            "input_summary":     s.input_summary[:512],
            "output_summary":    s.output_summary[:512],
            "latency_ms":        s.latency_ms,
            "retry_count":       s.retry_count,
            "error_type":        s.error_type,
            "validation_passed": s.validation_passed,
            "timestamp_iso":     s.timestamp_iso,
        }
        for s in steps
    ]

    def _sync() -> None:
        if _is_auth_failed():
            return  # Circuit open — skip silently
        client = _get_client(service_role=True)
        try:
            client.table("eval_steps").upsert(records).execute()
            logger.info("[EvalStore] Upserted %d eval_steps rows", len(records))
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[EvalStore] Could not upsert eval_steps: %s", exc)

    await asyncio.get_running_loop().run_in_executor(None, _sync)


async def upsert_run_metrics(metrics: EvalRunMetrics) -> None:
    """Upserts a single EvalRunMetrics row into ``eval_metrics``.

    Args:
        metrics: Computed metrics for the run.
    """
    record = {
        "run_id":               metrics.run_id,
        "success_rate":         metrics.success_rate,
        "exec_success_rate":    metrics.exec_success_rate,
        "validation_pass_rate": metrics.validation_pass_rate,
        "total_retries":        metrics.total_retries,
        "avg_debug_depth":      metrics.avg_debug_depth,
        "plan_step_count":      metrics.plan_step_count,
        "final_step_count":     metrics.final_step_count,
        "analyzer_ms":          metrics.analyzer_ms,
        "planner_ms":           metrics.planner_ms,
        "coder_ms":             metrics.coder_ms,
        "executor_ms":          metrics.executor_ms,
        "debugger_ms":          metrics.debugger_ms,
        "verifier_ms":          metrics.verifier_ms,
        "router_ms":            metrics.router_ms,
        "finalizer_ms":         metrics.finalizer_ms,
        "difficulty":           metrics.difficulty,
        "mode":                 metrics.mode,
        "query_length":         metrics.query_length,
    }

    def _sync() -> None:
        if _is_auth_failed():
            return  # Circuit open — skip silently
        client = _get_client(service_role=True)
        try:
            client.table("eval_metrics").upsert(record).execute()
            logger.info(
                "[EvalStore] Upserted eval_metrics for run_id=%s success_rate=%.2f",
                metrics.run_id,
                metrics.success_rate,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[EvalStore] Could not upsert eval_metrics: %s", exc)

    await asyncio.get_running_loop().run_in_executor(None, _sync)


# ---------------------------------------------------------------------------
# Read operations (used by eval_routes.py)
# ---------------------------------------------------------------------------

async def list_steps_for_run(run_id: str) -> List[Dict[str, Any]]:
    """Fetches all step rows for a single run, ordered by step_seq."""

    def _sync() -> List[Dict[str, Any]]:
        client = _get_client()
        try:
            resp = (
                client.table("eval_steps")
                .select("*")
                .eq("run_id", run_id)
                .order("step_seq")
                .execute()
            )
            return resp.data or []
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[EvalStore] Could not list eval_steps: %s", exc)
            return []

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def list_run_metrics(
    limit: int = 50,
    difficulty: str | None = None,
    mode: str | None = None,
) -> List[Dict[str, Any]]:
    """Fetches eval_metrics rows enriched with basic agent_run info.

    Uses a two-query approach instead of the PostgREST implicit-join syntax
    ``agent_runs(...)`` which requires a declared FK constraint in the schema
    cache (PGRST200 if the constraint is missing).  This makes the function
    resilient both before and after the FK migration is applied.

    Args:
        limit:      Max rows (1–100).
        difficulty: Optional filter — ``"easy"`` or ``"hard"``.
        mode:       Optional filter — ``"live"`` or ``"batch"``.

    Returns:
        List of eval_metrics rows ordered newest first, each optionally
        enriched with ``agent_runs`` sub-keys.
    """

    def _sync() -> List[Dict[str, Any]]:
        client = _get_client()

        # ── 1. Fetch eval_metrics (no join) ───────────────────────────────────
        try:
            q = (
                client.table("eval_metrics")
                .select("*")
                .order("created_at", desc=True)
                .limit(limit)
            )
            if difficulty:
                q = q.eq("difficulty", difficulty)
            if mode:
                q = q.eq("mode", mode)
            resp = q.execute()
            rows: List[Dict[str, Any]] = resp.data or []
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[EvalStore] Could not list eval_metrics: %s", exc)
            return []

        if not rows:
            return rows

        # ── 2. Enrich with agent_runs data in a separate query ────────────────
        run_ids = [r["run_id"] for r in rows if r.get("run_id")]
        run_map: Dict[str, Any] = {}
        if run_ids:
            try:
                runs_resp = (
                    client.table("agent_runs")
                    .select("id, query, status, created_at, completed_at, rounds")
                    .in_("id", run_ids)
                    .execute()
                )
                for run in (runs_resp.data or []):
                    run_map[run["id"]] = run
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning(
                    "[EvalStore] Could not enrich eval_metrics with agent_runs: %s", exc
                )

        # Merge: embed run info under the "agent_runs" key to preserve the
        # same response shape that the frontend / eval_routes already expect.
        for row in rows:
            row["agent_runs"] = run_map.get(row.get("run_id", ""), {})

        return rows

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def get_overview_stats() -> Dict[str, Any]:
    """Computes system-level overview KPIs from eval_metrics.

    Returns:
        Dict with ``total_runs``, ``success_rate``, ``avg_latency_ms``,
        ``avg_retries``, ``easy_count``, ``hard_count``.
    """

    def _sync() -> Dict[str, Any]:
        client = _get_client()
        try:
            resp = (
                client.table("eval_metrics")
                .select(
                    "success_rate, total_retries, difficulty, "
                    "analyzer_ms, planner_ms, coder_ms, executor_ms, "
                    "verifier_ms, router_ms, debugger_ms, finalizer_ms"
                )
                .execute()
            )
            rows = resp.data or []
            if not rows:
                return _empty_overview()

            n = len(rows)
            avg_success = sum(r["success_rate"] for r in rows) / n
            avg_retries = sum(r["total_retries"] for r in rows) / n
            avg_latency = sum(
                (r.get("coder_ms", 0) + r.get("executor_ms", 0) +
                 r.get("verifier_ms", 0) + r.get("planner_ms", 0))
                for r in rows
            ) / n
            easy = sum(1 for r in rows if r.get("difficulty") == "easy")
            hard = n - easy

            return {
                "total_runs":    n,
                "success_rate":  round(avg_success * 100, 1),
                "avg_latency_ms": round(avg_latency),
                "avg_retries":   round(avg_retries, 2),
                "easy_count":    easy,
                "hard_count":    hard,
            }
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[EvalStore] overview_stats error: %s", exc)
            return _empty_overview()

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


def _empty_overview() -> Dict[str, Any]:
    return {
        "total_runs": 0,
        "success_rate": 0.0,
        "avg_latency_ms": 0,
        "avg_retries": 0.0,
        "easy_count": 0,
        "hard_count": 0,
    }


async def get_agent_stats() -> List[Dict[str, Any]]:
    """Returns per-agent latency averages derived from eval_metrics rows.

    Returns:
        List of dicts ``{agent_name, avg_latency_ms, total_calls}``.
    """

    def _sync() -> List[Dict[str, Any]]:
        client = _get_client()
        try:
            resp = (
                client.table("eval_metrics")
                .select(
                    "analyzer_ms, planner_ms, coder_ms, executor_ms, "
                    "debugger_ms, verifier_ms, router_ms, finalizer_ms, "
                    "exec_success_rate, avg_debug_depth"
                )
                .execute()
            )
            rows = resp.data or []
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[EvalStore] agent_stats error: %s", exc)
            rows = []

        from eval.eval_engine import compute_agent_summary  # pylint: disable=import-outside-toplevel
        summary = compute_agent_summary(rows)
        return [
            {"agent_name": name, **stats}
            for name, stats in summary.items()
        ]

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def get_debug_loop_stats() -> Dict[str, Any]:
    """Returns debug loop aggregate statistics.

    Returns:
        Dict with ``avg_debug_depth``, ``error_type_distribution``,
        and ``retry_success_ratio``.
    """

    def _sync() -> Dict[str, Any]:
        client = _get_client()
        try:
            # Aggregate debug depth from eval_metrics
            resp_m = (
                client.table("eval_metrics")
                .select("avg_debug_depth, total_retries, exec_success_rate")
                .execute()
            )
            rows_m = resp_m.data or []

            # Error type distribution from eval_steps
            resp_s = (
                client.table("eval_steps")
                .select("error_type")
                .not_.is_("error_type", "null")
                .execute()
            )
            rows_s = resp_s.data or []
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[EvalStore] debug_loop_stats error: %s", exc)
            rows_m, rows_s = [], []

        n = len(rows_m) or 1
        avg_depth = sum(r.get("avg_debug_depth", 0) for r in rows_m) / n
        avg_retries = sum(r.get("total_retries", 0) for r in rows_m) / n
        avg_exec_success = sum(r.get("exec_success_rate", 0) for r in rows_m) / n

        # Build error type distribution
        error_dist: Dict[str, int] = {}
        for r in rows_s:
            et = r.get("error_type") or "Unknown"
            error_dist[et] = error_dist.get(et, 0) + 1

        return {
            "avg_debug_depth":    round(avg_depth, 2),
            "avg_retries":        round(avg_retries, 2),
            "retry_success_ratio": round(avg_exec_success, 4),
            "error_type_distribution": [
                {"error_type": et, "count": cnt}
                for et, cnt in sorted(
                    error_dist.items(), key=lambda x: -x[1]
                )[:10]
            ],
        }

    return await asyncio.get_running_loop().run_in_executor(None, _sync)
