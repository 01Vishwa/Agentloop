"""Tests for CodeExecutor — timeout handling, output capture, artifact collection."""

import os
import sys
import pytest

from core.executor.code_executor import CodeExecutor, ExecutionResult


# ---------------------------------------------------------------------------
# Basic execution — success path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_executor_simple_print():
    """Executor captures stdout from a simple Python script."""
    ex = CodeExecutor()
    result = await ex.run("print('hello ds-star')")

    assert result.success is True
    assert "hello ds-star" in result.stdout
    assert result.stderr == ""
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_executor_syntax_error():
    """Executor returns success=False and populates stderr on syntax errors."""
    ex = CodeExecutor()
    result = await ex.run("def broken(:\n    pass")

    assert result.success is False
    assert result.returncode != 0
    assert len(result.stderr) > 0


@pytest.mark.asyncio
async def test_executor_runtime_error():
    """Executor returns success=False on runtime exceptions."""
    ex = CodeExecutor()
    result = await ex.run("raise ValueError('test error')")

    assert result.success is False
    assert "ValueError" in result.stderr


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_executor_timeout(monkeypatch):
    """Executor catches TimeoutExpired and returns a descriptive error."""
    import subprocess
    from unittest.mock import patch

    original_timeout = __import__(
        "core.config", fromlist=["EXECUTION_TIMEOUT_SECONDS"]
    ).EXECUTION_TIMEOUT_SECONDS

    with patch("core.executor.code_executor.EXECUTION_TIMEOUT_SECONDS", 1):
        ex = CodeExecutor()
        # Sleep for longer than our patched 1-second timeout
        result = await ex.run("import time; time.sleep(10)")

    assert result.success is False
    assert "timed out" in result.stderr.lower() or "timeout" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Artifact collection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_executor_artifact_png():
    """Executor collects PNG files written to ./outputs/ and base64-encodes them."""
    code = """
import os, base64
os.makedirs('./outputs', exist_ok=True)
# Write a minimal 1x1 white PNG (89 bytes, valid PNG header)
png_bytes = bytes([
    137,80,78,71,13,10,26,10,0,0,0,13,73,72,68,82,
    0,0,0,1,0,0,0,1,8,2,0,0,0,144,119,83,222,
    0,0,0,12,73,68,65,84,8,215,99,248,255,255,63,0,
    5,254,2,254,220,204,89,231,0,0,0,0,73,69,78,68,
    174,66,96,130
])
with open('./outputs/test_plot.png', 'wb') as f:
    f.write(png_bytes)
print('done')
"""
    ex = CodeExecutor()
    result = await ex.run(code)

    assert result.success is True
    assert "test_plot.png" in result.artifacts
    # Verify it's valid base64
    import base64
    decoded = base64.b64decode(result.artifacts["test_plot.png"])
    assert decoded[:4] == b'\x89PNG'


@pytest.mark.asyncio
async def test_executor_artifact_csv():
    """Executor collects CSV files written to ./outputs/."""
    code = """
import os
os.makedirs('./outputs', exist_ok=True)
with open('./outputs/results.csv', 'w') as f:
    f.write('name,value\\nalpha,1\\nbeta,2\\n')
print('csv written')
"""
    ex = CodeExecutor()
    result = await ex.run(code)

    assert result.success is True
    assert "results.csv" in result.artifacts
    import base64
    content = base64.b64decode(result.artifacts["results.csv"]).decode("utf-8")
    assert "alpha" in content


@pytest.mark.asyncio
async def test_executor_no_artifacts_by_default():
    """Scripts that don't write to outputs/ yield an empty artifacts dict."""
    ex = CodeExecutor()
    result = await ex.run("print('no artifacts here')")

    assert result.artifacts == {}


# ---------------------------------------------------------------------------
# combined_output helper
# ---------------------------------------------------------------------------

def test_combined_output_both():
    """combined_output joins stdout and stderr with labels."""
    r = ExecutionResult("hello\n", "error\n", 1)
    out = r.combined_output()
    assert "[STDOUT]" in out
    assert "[STDERR]" in out


def test_combined_output_empty():
    """combined_output returns placeholder when both streams are empty."""
    r = ExecutionResult("", "", 0)
    assert r.combined_output() == "(no output)"
