"""Unit tests for the FinalizerAgent.

Mocks the LLM to verify deterministic schema enforcement and that noisy
execution outputs are cleanly formatted into markdown with citations.
"""

import pytest
from unittest.mock import AsyncMock, patch
from core.finalizer.finalizer_agent import FinalizerAgent, FinalizerOutput

@pytest.mark.asyncio
async def test_finalizer_agent_formats_output_correctly():
    """Tests that the agent correctly parses its output into the Pydantic schema."""

    # Sample schema matching what the structured LLM would return
    mock_output = FinalizerOutput(
        headline="Revenue grew by 15% in Q3",
        formatted_output=(
            "Based on the analysis, the revenue grew by 15% in Q3. "
            "See the attached charts: `chart.png`."
        ),
        confidence=0.95
    )

    # Mock the chain's ainvoke method to return our synthetic typed dict
    mock_chain = AsyncMock()
    mock_chain.ainvoke.return_value = mock_output

    with patch("core.finalizer.finalizer_agent.get_structured_llm") as mock_struct_llm:
        agent = FinalizerAgent()
        # Inject the mocked chain directly to bypass LangChain initialization
        agent._chain = mock_chain

        result = await agent.finalize(
            query="What was the Q3 revenue growth?",
            execution_output="q3_revenue = 1.15 * q2_revenue\\nprint(q3_revenue)",
            plan_steps=[{"index": 0, "description": "Calculate growth"}],
            artifact_names=["chart.png"]
        )

        assert result["headline"] == "Revenue grew by 15% in Q3"
        assert result["confidence"] == 0.95
        assert "`chart.png`" in result["formatted_output"]
        
        # Verify the prompt context was passed correctly
        call_args = mock_chain.ainvoke.call_args[0][0]
        assert call_args["query"] == "What was the Q3 revenue growth?"
        assert call_args["artifacts"] == "chart.png"


@pytest.mark.asyncio
async def test_finalizer_column_listing_uses_deterministic_format():
    """Column-listing queries should preserve exact column names from stdout."""
    agent = FinalizerAgent()
    agent._chain = AsyncMock()  # should not be used for this deterministic branch

    result = await agent.finalize(
        query="list down all columns here in this file",
        execution_output="healthcare_dataset.csv: ['Name', 'Age', 'Gender']",
        plan_steps=[{"index": 0, "description": "Print columns"}],
        artifact_names=[],
    )

    assert "Detected 3 column(s)" in result["headline"]
    assert "`healthcare_dataset.csv`" in result["formatted_output"]
    assert "`Name`" in result["formatted_output"]
    assert "`Age`" in result["formatted_output"]
    assert result["confidence"] == 1.0
    agent._chain.ainvoke.assert_not_called()
