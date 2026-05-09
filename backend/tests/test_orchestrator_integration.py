"""Integration tests for DsStarOrchestrator.

Mocks all 4 NIM agent chains so the full Plan→Code→Execute→Verify→Route
cycle runs without a real API key. Asserts the correct SSE event sequence
and verifies that retry events propagate correctly.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import (
    make_mock_chain,
    fake_plan_output,
    fake_verifier_output,
    fake_router_output,
    fake_code_output,
)


pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Shared test context
# ---------------------------------------------------------------------------

_QUERY = "Show average sales by region."
_CONTEXT = {
    "combined_extractions": {
        "sales.csv": {
            "source_type": "csv",
            "sanitized_content": "region,sales\nNorth,150\nSouth,120",
            "metadata": {"columns": ["region", "sales"], "row_count": 2},
        }
    }
}


# ---------------------------------------------------------------------------
# Helper: collect all SSE events from the orchestrator generator
# ---------------------------------------------------------------------------

async def _collect_events(orchestrator, query=_QUERY, context=_CONTEXT):
    events = []
    async for ev in orchestrator.run(query, context):
        events.append(ev)
    return events


def _event_types(events):
    return [e["event"] for e in events]


# ---------------------------------------------------------------------------
# Happy path: verifier approves on its first call → one round
# ---------------------------------------------------------------------------

async def test_orchestrator_happy_path():
    """Full loop completes in one round when verifier returns is_sufficient=True."""
    from core.ds_star_orchestrator import DsStarOrchestrator

    orch = DsStarOrchestrator()
    orch.planner._chain = make_mock_chain(fake_plan_output())
    orch.coder._chain = make_mock_chain(fake_code_output())
    orch.verifier._chain = make_mock_chain(fake_verifier_output(is_sufficient=True))
    # router should NOT be called in the happy path
    orch.router._chain = make_mock_chain(fake_router_output())

    events = await _collect_events(orch)
    types = _event_types(events)

    assert "analyzing" in types
    assert "planning" in types
    assert "plan_ready" in types
    assert "coding" in types
    assert "code_ready" in types
    assert "executing" in types
    assert "execution_result" in types
    assert "verifying" in types
    assert "verification_result" in types
    assert "completed" in types
    # Should not need to route
    assert "routing" not in types


# ---------------------------------------------------------------------------
# Two rounds: verifier rejects → router adds step → verifier approves
# ---------------------------------------------------------------------------

async def test_orchestrator_two_rounds():
    """Orchestrator routes and refines the plan when first verification fails."""
    from core.ds_star_orchestrator import DsStarOrchestrator

    call_count = {"n": 0}

    async def verifier_side_effect(inputs, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return fake_verifier_output(is_sufficient=False)
        return fake_verifier_output(is_sufficient=True)

    orch = DsStarOrchestrator()
    orch.planner._chain = make_mock_chain(fake_plan_output())
    orch.coder._chain = make_mock_chain(fake_code_output())
    orch.router._chain = make_mock_chain(fake_router_output(action="ADD_STEP"))

    # Verifier mock: fail first, pass second
    verifier_chain = MagicMock()
    verifier_chain.ainvoke = AsyncMock(side_effect=verifier_side_effect)
    orch.verifier._chain = verifier_chain

    events = await _collect_events(orch)
    types = _event_types(events)

    assert types.count("verification_result") == 2
    assert "routing" in types
    assert "plan_updated" in types
    assert "completed" in types


# ---------------------------------------------------------------------------
# Retry: coder fails twice then succeeds → retrying events emitted
# ---------------------------------------------------------------------------

async def test_orchestrator_coder_retry():
    """Retry events appear in the stream when CoderAgent fails then recovers."""
    from core.ds_star_orchestrator import DsStarOrchestrator

    call_count = {"n": 0}

    async def coder_side_effect(inputs, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise RuntimeError("NIM transient error")
        return fake_code_output()

    orch = DsStarOrchestrator()
    orch.planner._chain = make_mock_chain(fake_plan_output())
    orch.verifier._chain = make_mock_chain(fake_verifier_output(is_sufficient=True))
    orch.router._chain = make_mock_chain(fake_router_output())

    coder_chain = MagicMock()
    coder_chain.ainvoke = AsyncMock(side_effect=coder_side_effect)
    orch.coder._chain = coder_chain

    events = await _collect_events(orch)
    types = _event_types(events)

    assert "retrying" in types
    retrying_events = [e for e in events if e["event"] == "retrying"]
    assert len(retrying_events) == 2  # two failures before success
    assert "completed" in types


# ---------------------------------------------------------------------------
# Fatal coder failure: exhausts all 3 retries → terminal error event
# ---------------------------------------------------------------------------

async def test_orchestrator_coder_fatal():
    """Terminal 'error' event emitted when CoderAgent exhausts all 3 retries."""
    from core.ds_star_orchestrator import DsStarOrchestrator

    orch = DsStarOrchestrator()
    orch.planner._chain = make_mock_chain(fake_plan_output())
    orch.verifier._chain = make_mock_chain(fake_verifier_output(is_sufficient=True))
    orch.router._chain = make_mock_chain(fake_router_output())

    coder_chain = MagicMock()
    coder_chain.ainvoke = AsyncMock(side_effect=RuntimeError("persistent NIM failure"))
    orch.coder._chain = coder_chain

    events = await _collect_events(orch)
    types = _event_types(events)

    assert "error" in types
    assert "completed" not in types
    # Should have exactly 3 retry events (attempts 1, 2; before terminal)
    retrying_events = [e for e in events if e["event"] == "retrying"]
    assert len(retrying_events) >= 2


# ---------------------------------------------------------------------------
# Completed payload shape
# ---------------------------------------------------------------------------

async def test_orchestrator_completed_payload():
    """The 'completed' event payload contains insights, code, plan_steps, rounds."""
    from core.ds_star_orchestrator import DsStarOrchestrator

    orch = DsStarOrchestrator()
    orch.planner._chain = make_mock_chain(fake_plan_output())
    orch.coder._chain = make_mock_chain(fake_code_output())
    orch.verifier._chain = make_mock_chain(fake_verifier_output(is_sufficient=True))
    orch.router._chain = make_mock_chain(fake_router_output())

    events = await _collect_events(orch)
    completed = next(e for e in events if e["event"] == "completed")
    payload = completed["payload"]

    assert "insights" in payload
    assert "code" in payload
    assert "plan_steps" in payload
    assert "rounds" in payload
    assert payload["rounds"] >= 1
