"""FileAnalyzerAgent — Stage 1 of the DS-STAR pipeline.

DS-STAR Paper Implementation:
    The paper describes the Analyzer agent as generating a Python script via
    an LLM and *executing* it to extract key information (column types, sample
    rows, essential statistics) from each file.  This produces a compact but
    complete textual Data Description that grounds all subsequent agent prompts.

    This module implements that two-phase approach:
    1. LLM generates a file-introspection Python script per file.
    2. The script is executed in the sandbox; its stdout becomes the description.

    Fallback: If the LLM call or execution fails, the legacy static-formatting
    path is used so the pipeline never crashes on analyzer errors.
"""

import logging
from typing import Any, Dict

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

logger = logging.getLogger("uvicorn.info")


# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------

class AnalyzerScriptOutput(BaseModel):
    """A Python script that, when executed, prints a file's essential info."""

    script: str = Field(
        description=(
            "A complete, self-contained Python script that prints key information "
            "about the data file. No markdown fences — raw Python only."
        )
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_ANALYZER_SYSTEM = """\
You are an expert data scientist tasked with understanding data files.
Given a data file's type and its raw content preview, write a concise Python
script that PRINTS the most important structural information about the file.

The script will be executed with the file content available as a variable
named `_FILE_CONTENT_BYTES` (bytes) and `_FILE_CONTENT_STR` (decoded string).

FILE TYPE BRANCHING — read carefully:

For STRUCTURED files (CSV, XLSX, Parquet, JSON with tabular records):
  REQUIRED output sections (print each):
  1. "--- Essential Information ---"
  2. Data type label (e.g. CSV tabular)
  3. Column names and their dtypes
  4. Shape (rows x cols)
  5. First 5 rows as a formatted table
  6. Any detected anomalies (all-null columns, duplicate rows, obvious encoding issues)

For UNSTRUCTURED files (TXT, MD, Markdown, HTML, PDF, plain text):
  Do NOT attempt to find column names or data types — this file has no rows.
  REQUIRED output sections (print each):
  1. "--- Essential Information ---"
  2. File type label (e.g. Markdown document / Plain text)
  3. Total character count and estimated word count
  4. Number of sections/headings detected (for MD/HTML) or paragraph count (for TXT)
  5. First 500 characters as a text preview
  6. List of top-level headings or section titles (if present)
  7. Any detected anomalies (encoding errors, empty file, binary content)

Rules (apply to ALL types):
- Use only: pandas, json, io, re, and standard library.
- NEVER import from the filesystem — use _FILE_CONTENT_BYTES directly.
- Print clean, structured text. No tracebacks.
- Handle errors with try/except and print a note instead of crashing.
- Keep output under 2000 characters.
"""

_ANALYZER_HUMAN = """\
FILE NAME: {file_name}
SOURCE TYPE: {source_type}
CONTENT PREVIEW (first 2000 chars):
{content_preview}
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class FileAnalyzerAgent:
    """Generates a data description by running an LLM-generated inspection script.

    Stage 1 of the DS-STAR pipeline:
    - For each file, an LLM generates a Python introspection script.
    - The script is executed in the CodeExecutor sandbox.
    - The stdout is captured and assembled into the Data Description string.

    Falls back to the static formatter if LLM or execution fails.
    """

    def __init__(self) -> None:
        self._chain = None  # lazily built; reset on every analyze() call

    def _reset_chain(self) -> None:
        """Clears the cached LLM chain so the next call rebuilds with fresh context.

        Called at the top of ``analyze()`` on every invocation. Prevents stale
        column names from a previous file upload bleeding into code generated
        for the current session when the orchestrator singleton reuses this agent.
        """
        self._chain = None

    def _get_chain(self):
        """Builds and caches the LLM chain for script generation."""
        if self._chain is None:
            from core.llm_client import get_flash_structured_llm  # pylint: disable=import-outside-toplevel
            structured_llm = get_flash_structured_llm(AnalyzerScriptOutput, temperature=0.0)
            self._chain = (
                ChatPromptTemplate.from_messages([
                    ("system", _ANALYZER_SYSTEM),
                    ("human", _ANALYZER_HUMAN),
                ])
                | structured_llm
            )
        return self._chain

    async def analyze(self, combined_extractions: Dict[str, Any], session_id: str = "__anon__") -> str:
        """Builds a data description from the processing context.

        Resets the internal LLM chain at the start of each call so that
        column names from a previous file upload cannot pollute the description
        generated for the current request.

        For each file:
        1. Attempts to generate and execute an LLM inspection script.
        2. Falls back to static formatting if that fails.

        Args:
            combined_extractions: Dict keyed by filename, each value being a
                UnifiedDocumentContext-shaped dict from the parsers.

        Returns:
            Multi-section plain-English data description string.
        """
        # Fix 2 (P0): Reset chain on every analyze() call so stale column
        # context from a previous file/session does not persist.
        self._reset_chain()

        if not combined_extractions:
            return "No data files are available in the current context."

        sections = ["=== DATA DESCRIPTION ===\n"]

        for filename, doc in combined_extractions.items():
            if not isinstance(doc, dict):
                continue

            # Try LLM-based analysis first, then fall back
            section_text = await self._analyze_file_with_llm(filename, doc, session_id)
            if section_text is None:
                section_text = self._analyze_file_static(filename, doc)

            sections.append(section_text)

        description = "\n\n".join(sections)
        logger.info(
            "[FileAnalyzer] Generated data description (%d chars)", len(description)
        )
        return description

    async def analyze_multi(
        self,
        context,  # MultiFileContext
        session_id: str = "__anon__",
    ) -> str:
        """Builds a data description from a MultiFileContext.

        For single-file contexts, delegates to the standard ``analyze()`` path
        so the existing orchestrator behaviour is preserved exactly.

        For multi-file contexts (2+ files), prepends the compact
        ``MultiFileContext.to_prompt_str()`` summary and appends a multi-file
        analysis instruction so the LLM knows join keys are available.

        Args:
            context: A MultiFileContext instance from schema_merger.
            session_id: Session identifier for file content lookup.

        Returns:
            Multi-section data description string ready for agent prompts.
        """
        from models.multi_file_context import MultiFileContext  # pylint: disable=import-outside-toplevel

        files = context.files if context else []

        if not files:
            return "No data files are available in the current context."

        if len(files) == 1:
            # Single-file: fall through to the standard path
            # Build a minimal combined_extractions dict from the FileSchema
            fs = files[0]
            dummy_extraction = {
                "source_type": fs.file_type,
                "sanitized_content": "",
                "metadata": {
                    "columns": [c.name for c in fs.columns],
                    "dtypes": {c.name: c.dtype for c in fs.columns},
                    "row_count": fs.row_count,
                    "shape": [fs.row_count, len(fs.columns)],
                    "sample_rows": [
                        {c.name: (c.sample_values[0] if c.sample_values else None) for c in fs.columns}
                    ],
                },
            }
            return await self.analyze(
                {fs.file_name: dummy_extraction},
                session_id=session_id,
            )

        # Multi-file path
        self._reset_chain()

        # Build per-file descriptions using LLM or static fallback
        file_sections = []
        for fs in files:
            dummy_extraction = {
                "source_type": fs.file_type,
                "sanitized_content": "",
                "metadata": {
                    "columns": [c.name for c in fs.columns],
                    "dtypes": {c.name: c.dtype for c in fs.columns},
                    "row_count": fs.row_count,
                    "shape": [fs.row_count, len(fs.columns)],
                    "sample_rows": [
                        {c.name: (c.sample_values[0] if c.sample_values else None) for c in fs.columns}
                    ],
                },
            }
            section = await self._analyze_file_with_llm(fs.file_name, dummy_extraction, session_id)
            if section is None:
                section = self._analyze_file_static(fs.file_name, dummy_extraction)
            file_sections.append(section)

        # Compose multi-file description with join context header
        join_context_header = (
            "=== MULTI-FILE WORKSPACE ===\n"
            + context.to_prompt_str()
            + "\n\nYou are analysing a multi-file workspace. The user may ask questions "
            "that require joining data across files. The detected join candidates are "
            "listed above — treat these as probable but not certain. "
            "Flag any ambiguity in your analysis output."
        )

        description = join_context_header + "\n\n=== DATA DESCRIPTION ===\n\n" + "\n\n".join(file_sections)

        logger.info(
            "[FileAnalyzer] Multi-file description: %d files, %d chars",
            len(files),
            len(description),
        )
        return description


    async def _analyze_file_with_llm(
        self, filename: str, doc: Dict[str, Any], session_id: str = "__anon__"
    ) -> "str | None":
        """Generates and executes an LLM introspection script for one file.

        Args:
            filename: Name of the file being analyzed.
            doc: UnifiedDocumentContext dict from the parser.

        Returns:
            Description string on success, None on any failure.
        """
        try:
            import asyncio  # pylint: disable=import-outside-toplevel
            import subprocess  # pylint: disable=import-outside-toplevel
            import sys  # pylint: disable=import-outside-toplevel
            import tempfile  # pylint: disable=import-outside-toplevel
            import os  # pylint: disable=import-outside-toplevel

            source_type = doc.get("source_type", "unknown")
            content_preview = doc.get("sanitized_content", "")[:2000]

            # Generate the inspection script via LLM.
            chain = self._get_chain()
            result: AnalyzerScriptOutput = await asyncio.wait_for(
                chain.ainvoke({
                    "file_name": filename,
                    "source_type": source_type.upper(),
                    "content_preview": content_preview,
                }),
                timeout=20.0
            )

            script = result.script
            if script.startswith("```"):
                lines = script.split("\n")
                script = "\n".join(
                    line for line in lines if not line.strip().startswith("```")
                )

            # Inject file content into the script preamble.
            # First try the in-memory session cache; fall back to the workspace
            # disk path so files uploaded via /workspaces/{id}/upload are found
            # even when the session cache is keyed differently.
            from services.upload_service import get_file_content  # pylint: disable=import-outside-toplevel
            raw_bytes = get_file_content(filename, session_id=session_id) or b""

            if not raw_bytes:
                # Disk fallback: scan /workspace/ for the file
                workspace_base = os.environ.get("WORKSPACE_FILES_DIR", "/workspace")
                for _root, _dirs, _files in os.walk(workspace_base):
                    if filename in _files:
                        _candidate = os.path.join(_root, filename)
                        try:
                            with open(_candidate, "rb") as _fh:
                                raw_bytes = _fh.read()
                        except OSError:
                            pass
                        if raw_bytes:
                            logger.info(
                                "[FileAnalyzer] Loaded %s from disk fallback (%s).",
                                filename, _candidate,
                            )
                        break

            if not raw_bytes:
                # No data anywhere — skip expensive LLM path, use static fallback
                logger.warning(
                    "[FileAnalyzer] No content found for %s (session=%s); "
                    "using static fallback.",
                    filename, session_id,
                )
                return None

            preamble = (
                "import sys, io, json, re\n"
                "import pandas as _pd\n"
                f"_FILE_CONTENT_BYTES = {repr(raw_bytes[:200_000])}\n"
                f"_FILE_CONTENT_STR = _FILE_CONTENT_BYTES.decode('utf-8', errors='replace')\n\n"
            )
            full_script = preamble + script

            # Execute in a minimal subprocess with sanitised environment.
            # GAP-04 fix: include Windows-specific vars (SystemDrive, APPDATA,
            # LOCALAPPDATA) required for Python DLL resolution on Windows.
            safe_env = {
                k: v for k, v in os.environ.items()
                if k in {
                    "PATH", "PYTHONPATH", "HOME", "USERPROFILE", "SYSTEMROOT",
                    "SystemDrive", "APPDATA", "LOCALAPPDATA",
                    "TEMP", "TMP", "LANG", "LC_ALL",
                }
            }
            safe_env["PYTHONDONTWRITEBYTECODE"] = "1"

            with tempfile.TemporaryDirectory() as tmpdir:
                script_path = os.path.join(tmpdir, "analyzer_script.py")
                with open(script_path, "w", encoding="utf-8") as fh:
                    fh.write(full_script)

                proc = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, script_path],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    cwd=tmpdir,
                    env=safe_env,
                )

            output = proc.stdout.strip() or proc.stderr.strip()
            if not output:
                return None

            return f"--- File: {filename} (type: {source_type.upper()}) ---\n{output}"

        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "[FileAnalyzer] LLM analysis failed for %s (%s); using static fallback.",
                filename,
                exc,
            )
            return None

    def _analyze_file_static(
        self, filename: str, doc: Dict[str, Any]
    ) -> str:
        """Static fallback: builds description from parser metadata keys.

        Args:
            filename: Name of the file.
            doc: UnifiedDocumentContext dict.

        Returns:
            Formatted description string.
        """
        source_type = doc.get("source_type", "unknown").upper()
        content = doc.get("sanitized_content", "")
        metadata = doc.get("metadata", {})

        section = [f"--- File: {filename} (type: {source_type}) ---"]

        if "columns" in metadata:
            section.append(f"  Columns     : {metadata['columns']}")
        if "dtypes" in metadata:
            section.append(f"  Data Types  : {metadata['dtypes']}")
        if "shape" in metadata:
            section.append(f"  Shape       : {metadata['shape']}")
        if "row_count" in metadata:
            section.append(f"  Row Count   : {metadata['row_count']}")
        if "sample_rows" in metadata:
            section.append(f"  Sample Rows : {metadata['sample_rows']}")
        if "keys" in metadata:
            section.append(f"  JSON Keys   : {metadata['keys']}")
        if "pages" in metadata:
            section.append(f"  Pages       : {metadata['pages']}")
        if "sheet_names" in metadata:
            section.append(f"  Sheets      : {metadata['sheet_names']}")

        if content:
            if source_type in {"PDF", "TXT", "MD", "MARKDOWN", "UNKNOWN"}:
                full_text = content[:6000]
                section.append(
                    "  Full Text Content (use this directly — do NOT open the file):\n"
                    f"{full_text}"
                )
            else:
                preview = content[:4000].replace("\n", " ")
                section.append(f"  Content Preview: {preview}")

        if "error" in metadata:
            section.append(f"  ⚠ Parse Error: {metadata['error']}")

        return "\n".join(section)
