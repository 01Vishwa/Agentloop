"""CoderAgent — translates the current plan into executable Python.

Uses NVIDIA NIM with ``.with_structured_output`` for hard schema-compliance
where available, and raw-completion + extraction for models (e.g. CodeLlama,
Llama-Instruct) that cannot reliably produce function-calling or json_mode
structured output.

Gap fixes applied:
- Coder prompt reframed as accumulative/sequential (DS-STAR "Colab notebook"
  model): the agent EXTENDS the previous script, not rewrites it from scratch.
- Task-type routing added: coder adapts output mode to ML / Wrangling /
  Visualization / Insight.
- execution_output capped at 3 000 chars before being sent to the LLM.
- Defensive strip of markdown fences retained.
- [v3] CodeLlama-family models now use a raw-completion primary path instead
  of json_mode structured output, eliminating column-key retry loops.
  The extractor now also handles multi-field JSON by selecting the longest
  string value, making it resilient to hallucinated key names.
- [v4] Raw-completion bypass extended to Llama-3.x / Llama-4 Instruct families
  via a wildcard matcher; once a structured-output failure is observed we
  flip the agent into sticky raw-completion mode for the rest of the run.
  The extractor is now schema-aware: when the JSON's wrong key matches a
  known data-column name (e.g. {"<column_name>": "import pandas..."}), we
  recover the code from that field instead of raising.
"""

import asyncio
import logging
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from core.token_tracker import TokenTracker, tracker_callback_config

logger = logging.getLogger("uvicorn.info")

# Maximum chars of execution output passed to the coder
_MAX_EXEC_OUTPUT_CHARS = 3000

# ---------------------------------------------------------------------------
# Raw-completion model selection
#
# Some NIM-hosted models cannot reliably emit `{"code": "..."}` under
# json_mode structured output — they hallucinate wrong JSON keys (e.g.
# `{"<column_name>": "..."}`) when the prompt mentions data-column names. For those
# models we skip structured output entirely and parse the raw completion.
#
# Match rules (any of):
#   1. ``meta/codellama-*``               (no function-calling support)
#   2. ``meta/llama-3.x-...-instruct``    (3.1 / 3.2 / 3.3 — observed bug)
#   3. ``meta/llama-4-...-instruct``      (future-proof)
#   4. Any model listed in the comma-separated env override
#      ``CODER_RAW_COMPLETION_MODELS`` (case-insensitive exact match).
# ---------------------------------------------------------------------------

_RAW_COMPLETION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^meta/codellama-.+$", re.IGNORECASE),
    # Llama 3.x Instruct (covers 3, 3.1, 3.2, 3.3 and any size suffix)
    re.compile(r"^meta/llama-3[.\-].+-instruct$", re.IGNORECASE),
    # Llama 4 Instruct (future-proof)
    re.compile(r"^meta/llama-4[.\-].+-instruct$", re.IGNORECASE),
)


def _env_raw_completion_overrides() -> frozenset[str]:
    """Returns the lowercase model identifiers from ``CODER_RAW_COMPLETION_MODELS``.

    Re-read on every call so test harnesses (and operators tweaking the env
    at runtime) see fresh values without restarting the process.
    """
    raw = os.getenv("CODER_RAW_COMPLETION_MODELS", "")
    return frozenset(
        item.strip().lower()
        for item in raw.split(",")
        if item.strip()
    )


def _should_use_raw_completion(model: Optional[str]) -> bool:
    """Decides whether a given model should bypass structured output.

    Args:
        model: Resolved NIM model identifier.

    Returns:
        True when the raw-completion path must be used.
    """
    if not model:
        return False
    if model.lower() in _env_raw_completion_overrides():
        return True
    return any(pat.match(model) for pat in _RAW_COMPLETION_PATTERNS)


# ---------------------------------------------------------------------------
# Schema-hint helpers
# ---------------------------------------------------------------------------

# Tokens that strongly suggest a string value contains real Python code.
_PY_SIGNAL_TOKENS: tuple[str, ...] = (
    "import ",
    "def ",
    "print(",
    "pd.read_",
    "df[",
    "df.",
    "plt.",
    "np.",
    "for ",
    "if __name__",
)


def _looks_like_python(text: str) -> bool:
    """Heuristic: does this string contain unambiguous Python tokens?"""
    if not text or len(text) < 10:
        return False
    return any(tok in text for tok in _PY_SIGNAL_TOKENS)


def _parse_columns_from_schema_hints(schema_hints: str) -> List[str]:
    """Extracts a flat list of column names from the orchestrator's hint string.

    The orchestrator passes hints in the form
    ``"dataset.csv: ['<col_a>', '<col_b>', ...]"`` (one file per line).
    Returns the union of all column names across files, lower-cased and
    de-duplicated. Used by the extractor to recognise misnamed JSON keys.
    """
    if not schema_hints or schema_hints.strip().lower() in {"(unknown)", "(none provided)"}:
        return []

    cols: list[str] = []
    for match in re.finditer(r"\[([^\]]+)\]", schema_hints):
        body = match.group(1)
        for raw in body.split(","):
            name = raw.strip().strip("'\"`").strip()
            if name and name.lower() not in {c.lower() for c in cols}:
                cols.append(name)
    return cols


def _is_column_listing_query(query: str) -> bool:
    """Returns True when the user asks to list/show dataset column names."""
    q = (query or "").strip().lower()
    if not q:
        return False
    has_column_word = any(w in q for w in ("column", "columns", "header", "headers", "field", "fields"))
    has_listing_intent = any(
        w in q for w in ("list", "show", "print", "display", "what are", "name", "names")
    )
    return has_column_word and has_listing_intent


def _is_data_distribution_query(query: str) -> bool:
    """Returns True when the user asks about data distribution/summary."""
    q = (query or "").strip().lower()
    if not q:
        return False
    return any(
        kw in q
        for kw in (
            "distribution", "describe", "summary", "statistics",
            "histogram", "shape", "info", "overview", "profile",
            "basic stats", "data exploration", "explore",
        )
    )


def _build_distribution_fallback_script() -> str:
    """Builds a deterministic data-distribution script.

    Used as an emergency fallback when the LLM endpoint repeatedly fails.
    Discovers all tabular files in the working directory, prints .describe(),
    value counts for categorical columns, and saves interactive Plotly HTML
    histograms for numeric ones.

    Uses plotly (pre-installed) instead of matplotlib (not available in sandbox).

    IMPORTANT: This function must produce a syntactically valid Python script.
    Avoid embedding literal newlines inside f-string quotes — use separate
    print() calls or string concatenation instead.
    """
    return (
        "import os\n"
        "import pandas as pd\n"
        "\n"
        "# Use plotly for visualization — it is pre-installed in the sandbox.\n"
        "# matplotlib is NOT available; never import it without an explicit guard.\n"
        "try:\n"
        "    import plotly.express as px\n"
        "    import plotly.subplots as sp\n"
        "    import plotly.graph_objects as go\n"
        "    _HAS_PLOTLY = True\n"
        "except ImportError:\n"
        "    _HAS_PLOTLY = False\n"
        "    print('plotly not available — skipping chart generation')\n"
        "\n"
        "os.makedirs('./outputs', exist_ok=True)\n"
        "\n"
        "TABULAR_EXTS = ('.csv', '.xlsx', '.xls', '.parquet', '.json')\n"
        "files = sorted(\n"
        "    f for f in os.listdir('.')\n"
        "    if os.path.isfile(f) and f.lower().endswith(TABULAR_EXTS)\n"
        ")\n"
        "\n"
        "if not files:\n"
        "    print('No tabular data files found in working directory.')\n"
        "else:\n"
        "    for fname in files:\n"
        "        try:\n"
        "            lower = fname.lower()\n"
        "            if lower.endswith('.csv'):\n"
        "                df = pd.read_csv(fname)\n"
        "            elif lower.endswith(('.xlsx', '.xls')):\n"
        "                df = pd.read_excel(fname)\n"
        "            elif lower.endswith('.parquet'):\n"
        "                df = pd.read_parquet(fname)\n"
        "            elif lower.endswith('.json'):\n"
        "                df = pd.read_json(fname)\n"
        "            else:\n"
        "                continue\n"
        "\n"
        "            print('')\n"
        "            print('=== ' + fname + ' ===')\n"
        "            print('Shape: ' + str(df.shape))\n"
        "            print('Columns: ' + str(df.columns.tolist()))\n"
        "            print('Dtypes:')\n"
        "            print(df.dtypes.to_string())\n"
        "            print('')\n"
        "            print('Descriptive Statistics:')\n"
        "            print(df.describe(include='all').to_string())\n"
        "\n"
        "            # ── Plotly: numeric distributions ──\n"
        "            if _HAS_PLOTLY:\n"
        "                num_cols = df.select_dtypes(include='number').columns.tolist()\n"
        "                if num_cols:\n"
        "                    n_plots = min(len(num_cols), 6)\n"
        "                    cols_to_plot = num_cols[:n_plots]\n"
        "                    fig = sp.make_subplots(\n"
        "                        rows=n_plots, cols=1,\n"
        "                        subplot_titles=['Distribution of ' + c for c in cols_to_plot],\n"
        "                        vertical_spacing=0.06,\n"
        "                    )\n"
        "                    for i, col in enumerate(cols_to_plot, start=1):\n"
        "                        series = df[col].dropna()\n"
        "                        fig.add_trace(\n"
        "                            go.Histogram(x=series, nbinsx=30, name=col,\n"
        "                                         marker_color='#636EFA'),\n"
        "                            row=i, col=1,\n"
        "                        )\n"
        "                        fig.update_xaxes(title_text=col, row=i, col=1)\n"
        "                        fig.update_yaxes(title_text='Frequency', row=i, col=1)\n"
        "                    fig.update_layout(\n"
        "                        title_text='Data Distribution: ' + fname,\n"
        "                        height=350 * n_plots,\n"
        "                        showlegend=False,\n"
        "                        template='plotly_white',\n"
        "                    )\n"
        "                    base = fname.rsplit('.', 1)[0]\n"
        "                    chart_name = 'distribution_' + base + '.html'\n"
        "                    fig.write_html('./outputs/' + chart_name)\n"
        "                    print('')\n"
        "                    print('Interactive distribution chart saved to ./outputs/' + chart_name)\n"
        "\n"
        "                # ── Plotly: categorical value counts ──\n"
        "                cat_cols = [c for c in df.columns\n"
        "                            if df[c].dtype == object or str(df[c].dtype) == 'string']\n"
        "                for col in cat_cols[:4]:\n"
        "                    vc = df[col].value_counts().head(15)\n"
        "                    fig_cat = px.bar(\n"
        "                        x=vc.index.astype(str), y=vc.values,\n"
        "                        labels={'x': col, 'y': 'Count'},\n"
        "                        title='Value Counts: ' + col,\n"
        "                        template='plotly_white',\n"
        "                    )\n"
        "                    cat_chart = 'valuecounts_' + col[:30] + '_' + base + '.html'\n"
        "                    fig_cat.write_html('./outputs/' + cat_chart)\n"
        "                    print('Category chart saved to ./outputs/' + cat_chart)\n"
        "            else:\n"
        "                # Plain-text fallback when plotly is also missing\n"
        "                cat_cols = df.select_dtypes(include='object').columns.tolist()\n"
        "                for col in cat_cols[:5]:\n"
        "                    print('')\n"
        "                    print(\"Value counts for '\" + col + \"':\")\n"
        "                    print(df[col].value_counts().head(10).to_string())\n"
        "\n"
        "        except Exception as exc:\n"
        "            print(str(fname) + ': failed to process (' + str(exc) + ')')\n"
        "\n"
        "print('')\n"
        "print('Data distribution analysis complete.')\n"
    )



def _build_column_listing_fallback_script(known_columns: Sequence[str]) -> str:
    """Builds a deterministic script that prints columns from local tabular files.

    Used only as an emergency fallback when the LLM endpoint repeatedly fails
    before returning any text. This keeps DS-STAR functional for the common
    "list columns" request class.
    """
    schema_cols_literal = repr(list(known_columns))
    return f"""import json
import os
import pandas as pd

TABULAR_EXTS = ('.csv', '.xlsx', '.xls', '.parquet', '.json')
files = sorted(
    f for f in os.listdir('.')
    if os.path.isfile(f) and f.lower().endswith(TABULAR_EXTS)
)

if not files:
    print("No tabular data files found in working directory.")
    fallback_cols = {schema_cols_literal}
    if fallback_cols:
        print("Columns (from schema hints):", fallback_cols)
else:
    found_any = False
    for fname in files:
        try:
            lower = fname.lower()
            if lower.endswith('.csv'):
                df = pd.read_csv(fname)
            elif lower.endswith(('.xlsx', '.xls')):
                df = pd.read_excel(fname)
            elif lower.endswith('.parquet'):
                df = pd.read_parquet(fname)
            elif lower.endswith('.json'):
                with open(fname, 'r', encoding='utf-8', errors='replace') as fh:
                    obj = json.load(fh)
                if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                    df = pd.DataFrame(obj)
                elif isinstance(obj, dict):
                    df = pd.DataFrame([obj])
                else:
                    print(f"{{fname}}: unsupported JSON structure for column listing")
                    continue
            else:
                continue

            found_any = True
            print(f"{{fname}}: {{df.columns.tolist()}}")
        except Exception as exc:
            print(f"{{fname}}: failed to read ({{exc}})")

    if not found_any:
        fallback_cols = {schema_cols_literal}
        if fallback_cols:
            print("Columns (from schema hints):", fallback_cols)
"""


# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------

class CodeOutput(BaseModel):
    """Generated Python script output."""

    code: str = Field(
        description=(
            "Complete, self-contained Python script. "
            "No markdown fences, no explanations — raw Python only. "
            "NEVER include the final answer here. ONLY include the code."
        )
    )


def _extract_code_from_model_text(
    text: str,
    known_columns: Optional[Sequence[str]] = None,
) -> str:
    """Best-effort extraction of raw Python code from an LLM response.

    The Coder is *supposed* to return ``{"code": "..."}`` but some models
    emit a JSON object with the wrong key (e.g. ``{"<column_name>": "..."}`` or
    ``{"distribution": "..."}``). We recover via the following priority order:

    1. Canonical ``code`` key.
    2. Known aliases (``script``, ``python``, ``py``, ``content``, ``result``,
       ``output``, ``answer``).
    3. **Schema-aware recovery**: if the JSON has exactly one string field
       and its key matches (case-insensitive) any name in ``known_columns``,
       return that value. This is the column-name hallucination case.
    4. **Python-signal preference**: among string fields, pick the value
       that contains unambiguous Python tokens (``import``, ``def``,
       ``print(``, ``df.``, ``plt.`` …) — even if it isn't the longest.
    5. If only one string field exists, use it.
    6. Fallback: pick the **longest** string value.

    Args:
        text: Raw LLM completion text (may be JSON, code, or noise).
        known_columns: Optional list of column names from the data schema.
            When provided, makes the extractor robust to column-name JSON keys.

    Returns:
        Extracted Python source, or an empty string if nothing usable found.
    """
    if not text:
        return ""

    s = text.strip()

    # Remove markdown fences if present
    if s.startswith("```"):
        lines = s.splitlines()
        s = "\n".join(line for line in lines if not line.strip().startswith("```")).strip()

    # Try JSON parsing (common for json_mode responses)
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                if isinstance(obj.get("code"), str):
                    return obj["code"].strip()

                # Common aliases — explicit catalogue covers most stray models.
                for k in (
                    "script", "python", "py", "content",
                    "result", "output", "answer",
                ):
                    if isinstance(obj.get(k), str):
                        return obj[k].strip()

                string_items = [(k, v) for k, v in obj.items() if isinstance(v, str)]

                # Schema-aware recovery: a single string field whose key is a
                # known data-column name => the value IS the misnamed code.
                if known_columns and len(string_items) == 1:
                    only_key, only_val = string_items[0]
                    col_lower = {c.lower() for c in known_columns}
                    if only_key.lower() in col_lower:
                        logger.warning(
                            "[Coder] JSON key '%s' is a data-column name; "
                            "treating value as code (%d chars).",
                            only_key, len(only_val),
                        )
                        return only_val.strip()

                # Python-signal preference: pick the field whose value most
                # clearly looks like real Python.
                python_like = [
                    (k, v) for k, v in string_items if _looks_like_python(v)
                ]
                if python_like:
                    if len(python_like) > 1:
                        # Tie-break by length when several fields look Pythonic.
                        python_like.sort(key=lambda kv: len(kv[1]), reverse=True)
                    chosen_k, chosen_v = python_like[0]
                    logger.warning(
                        "[Coder] JSON key '%s' not canonical — using "
                        "Python-like field (%d chars).",
                        chosen_k, len(chosen_v),
                    )
                    return chosen_v.strip()

                if len(string_items) == 1:
                    return string_items[0][1].strip()

                # Fallback: pick the longest string value (almost always the script)
                if string_items:
                    longest = max(string_items, key=lambda kv: len(kv[1]))
                    logger.warning(
                        "[Coder] JSON keys %s not recognised — using longest field '%s' (%d chars).",
                        list(obj.keys()), longest[0], len(longest[1]),
                    )
                    return longest[1].strip()
        except Exception:  # pylint: disable=broad-except
            pass

    return s


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_CODER_SYSTEM = """\
You are an expert Python data scientist working like a Jupyter/Colab notebook.

Your task is to generate a single, self-contained Python script that implements
the CURRENT analysis plan.

CRITICAL SCHEMA INSTRUCTION: 
You MUST output a valid function call with a SINGLE field named `code`.
Do NOT try to answer the user's query directly by creating new fields (e.g., if the user asks about a dataset column, DO NOT output that column name as a JSON key).
Your ONLY job is to write the Python script. The script itself will compute and print the answer.

EXECUTION MODEL — Read this carefully:
- If PREVIOUS CODE is provided, you are in a refinement round.
- You must EXTEND the previous script by adding NEW sections at the end,
  OR correct a broken section.  Do NOT discard working code.
- The script runs from top to bottom each round, so all imports and data-loading
  stay at the top; new analysis blocks go at the end.

TASK TYPE OUTPUT MODES:
- Insight / Data Analysis: print() final numeric answers clearly.
- Visualization: save interactive charts with fig.write_html('./outputs/<name>.html')
  (uses plotly — matplotlib is NOT available).
- Data Wrangling: save cleaned data with df.to_csv('./outputs/<name>.csv', index=False)
- Machine Learning: save the model with joblib.dump(model, './outputs/model.joblib')
  AND print metrics (accuracy, RMSE, etc.)

GENERAL RULES:
- PRE-INSTALLED PACKAGES (use freely): pandas, numpy, scipy, sklearn, joblib,
  plotly, Pillow (PIL), and the Python standard library.
- NOT INSTALLED — NEVER import these: matplotlib, seaborn, statsmodels.
  If you must use visualization, always use plotly (plotly.express or plotly.graph_objects).
- Read files by filename — files are pre-injected into the working directory.
- For plots: use plotly.express or plotly.graph_objects. Save with
  fig.write_html('./outputs/<name>.html') OR fig.write_image('./outputs/<name>.png')
  (write_image requires kaleido — prefer write_html which always works).
  NEVER call plt.show() or import matplotlib.
- The ./outputs/ directory is pre-created.
- Handle missing values gracefully with pd.to_numeric(..., errors='coerce').
- NEVER use deprecated NumPy aliases: np.object, np.int, np.float, np.bool.
  Use built-in types or np.object_, np.int64, np.float64, np.bool_.
- Before correlation/statistics, check for zero variance:
    if df['col'].std() == 0: print("Zero variance — cannot compute correlation")
- If a result is NaN, always print WHY (zero variance, all-null, etc.).
- The script must print a clear final answer or summary as the last action.

COLUMN SAFETY — mandatory steps at the top of EVERY script that loads data:
1. After loading any file into a DataFrame, ALWAYS print:
     print("Columns:", df.columns.tolist())
     print("Shape:", df.shape)
     print("Dtypes:\n", df.dtypes)
   This ensures the real schema is visible if there is a column mismatch.
2. Before accessing ANY column by name, validate it exists:
     if 'col_name' not in df.columns:
         print("WARNING: 'col_name' not found. Available columns:", df.columns.tolist())
         # Use the closest available column or skip the step gracefully
   NEVER assume a column exists based on the data description — always verify.
   CRITICAL: ONLY use columns explicitly listed in the DATA DESCRIPTION. Do not invent or hallucinate column names.
3. Use case-insensitive column lookup when appropriate:
     cols_lower = dict((c.lower(), c) for c in df.columns)
     actual_col = cols_lower.get('target_col_lower')
     if actual_col is None:
         print("Column not found (case-insensitive). Available:", df.columns.tolist())

IMPORTANT — Robust data handling:
- Coerce numerics: pd.to_numeric(df['col'], errors='coerce')
- Drop NaN before stats: df.dropna(subset=['col1', 'col2'])
- NEVER silently output NaN as the final result.
"""

_CODER_HUMAN = """\
USER QUERY:
{query}

DATA DESCRIPTION:
{data_description}

AVAILABLE COLUMNS (SCHEMA HINTS — these are DATA COLUMN NAMES, not output keys):
{schema_hints}

CURRENT ANALYSIS PLAN:
{plan_steps}

PREVIOUS CODE (extend this — do not discard working sections):
{previous_code}

LAST EXECUTION OUTPUT (errors to fix are highlighted here):
{execution_output}

=== OUTPUT FORMAT (MANDATORY — READ CAREFULLY) ===
Respond with ONE JSON object whose ONLY key is the LITERAL STRING: code
The value of that key must be the complete Python script as a string.

CORRECT:
    {{"code": "import pandas as pd\\nprint('ok')"}}

WRONG — do NOT produce any of these shapes (every one of these will be REJECTED):
    {{"<column_name>": "import pandas..."}}  ← data column names are never output keys
    {{"distribution": "import pandas..."}}   ← any topic word is forbidden as the key
    {{"answer": "..."}}                      ← only "code" is valid
    {{"code": "...", "explanation": "..."}}  ← exactly ONE key allowed

The data column names listed under SCHEMA HINTS above must NEVER appear as the JSON key.
"code" is the OUTPUT KEY NAME — it is unrelated to any column in the dataset.

Do NOT wrap the JSON in markdown fences. Do NOT add any explanation outside the JSON.
===============================================
"""

_CODER_HUMAN_RAW = """\
USER QUERY:
{query}

DATA DESCRIPTION:
{data_description}

AVAILABLE COLUMNS (SCHEMA HINTS):
{schema_hints}

CURRENT ANALYSIS PLAN:
{plan_steps}

PREVIOUS CODE (extend this — do not discard working sections):
{previous_code}

LAST EXECUTION OUTPUT (errors to fix are highlighted here):
{execution_output}

=== OUTPUT FORMAT (MANDATORY FOR RAW MODE) ===
Return ONLY the complete Python script as plain text.
Do NOT wrap in JSON.
Do NOT wrap in markdown fences.
Do NOT include explanations before or after the code.
===============================================
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class CoderAgent:
    """Translates plan steps into a runnable Python script via NIM.

    Uses ``.with_structured_output`` to enforce schema compliance at the
    function-calling protocol level.
    """

    def __init__(
        self, model: Optional[str] = None, temperature: Optional[float] = None
    ) -> None:
        """Initialises the agent.

        Args:
            model: NIM model identifier; defaults to ``NIM_MODEL_CODER``.
            temperature: Sampling temperature; defaults to 0.1.
        """
        self._model = model
        self._temperature = temperature if temperature is not None else 0.1
        self._chain = None  # lazily built
        self._lock = asyncio.Lock()  # async-safe lazy init (avoids event-loop deadlock)
        # Sticky raw-completion mode: once flipped True, every subsequent
        # generate_code() call uses the raw path — we never go back to
        # structured output for the rest of this Coder instance's life.
        self._force_raw: bool = False
        self._mode_locked: bool = False

    def _get_resolved_model(self) -> str:
        """Returns the resolved model name, defaulting to NIM_MODEL_CODER."""
        from core.config import NIM_MODEL_CODER  # pylint: disable=import-outside-toplevel
        return self._model or NIM_MODEL_CODER

    def _is_raw_path(self) -> bool:
        """Decides — for the current call — whether to use raw completion."""
        return self._force_raw or _should_use_raw_completion(self._get_resolved_model())

    def force_raw_completion(self, reason: str = "") -> None:
        """Switches the agent into sticky raw-completion mode for the rest of the run.

        After this call, the cached chain is invalidated so the next
        ``generate_code`` invocation rebuilds the pipeline without the
        structured-output binding. Subsequent calls keep using raw completion
        even on different queries within the same orchestrator run.

        Idempotent: repeated calls are a no-op.

        Args:
            reason: Human-readable reason for the switch (logged for debugging).
        """
        if self._mode_locked and self._force_raw:
            return
        self._force_raw = True
        self._mode_locked = True
        self._chain = None  # force rebuild on the next generate_code call
        logger.warning(
            "[Coder] Sticky raw-completion mode ENGAGED (reason=%s, model=%s).",
            reason or "unspecified",
            self._get_resolved_model(),
        )

    def _build_chain(self):
        """Builds (but does NOT cache) the LangChain pipeline.

        Heavy work — LLM initialisation, imports — happens here, OUTSIDE the
        async lock, so the event loop is never blocked during first-call setup.

        For models matched by ``_should_use_raw_completion`` OR when the
        sticky ``self._force_raw`` flag is set, we skip structured output
        and use a plain raw-completion chain. The extractor
        ``_extract_code_from_model_text`` then pulls the code from whatever
        the model returns — even if it uses the wrong JSON key.

        For other models we force JSON-mode structured output.
        """
        resolved = self._get_resolved_model()
        if self._is_raw_path():
            prompt = ChatPromptTemplate.from_messages([
                ("system", _CODER_SYSTEM),
                ("human", _CODER_HUMAN_RAW),
            ])
            # Raw-completion path: no structured output binding.
            from core.llm_client import get_nim_llm  # pylint: disable=import-outside-toplevel
            raw_llm = get_nim_llm(
                model=resolved,
                temperature=self._temperature,
                cache_scope="raw",
                use_cache=False,
            )
            chain = prompt | raw_llm
            logger.info(
                "[Coder] Using raw-completion chain for model=%s "
                "(structured output unreliable; force_raw=%s).",
                resolved,
                self._force_raw,
            )
        else:
            prompt = ChatPromptTemplate.from_messages([
                ("system", _CODER_SYSTEM),
                ("human", _CODER_HUMAN),
            ])
            # Structured-output path for capable models.
            from core.llm_client import get_structured_llm_with_mode  # pylint: disable=import-outside-toplevel
            structured_llm = get_structured_llm_with_mode(
                model=resolved,
                schema=CodeOutput,
                temperature=self._temperature,
                force_json_mode=True,
            )
            chain = prompt | structured_llm
        return chain

    async def generate_code(
        self,
        query: str,
        data_description: str,
        plan_steps: List[Dict[str, Any]],
        previous_code: str = "",
        execution_output: str = "",
        schema_hints: str = "",
        token_tracker: Optional[TokenTracker] = None,
    ) -> str:
        """Generates a Python script implementing the analysis plan.

        Args:
            query: The user's natural language question.
            data_description: Output of FileAnalyzerAgent.
            plan_steps: Current plan steps.
            previous_code: Code from the previous round (if any).
            execution_output: stdout/stderr from the previous execution.
            schema_hints: Extracted strict column names to prevent hallucination.
            token_tracker: Optional run-level tracker.  When provided, token
                usage from this LLM call is recorded automatically via a
                LangChain callback.

        Returns:
            A self-contained Python script as a string.
        """
        formatted_steps = "\n".join(
            f"  Step {s['index'] + 1}: {s['description']}"
            for s in plan_steps
        )

        # Cap execution output passed to coder to avoid context window exhaustion
        trimmed_exec = execution_output[:_MAX_EXEC_OUTPUT_CHARS] if execution_output else "(none)"

        invoke_input = {
            "query": query,
            "data_description": data_description,
            "schema_hints": schema_hints or "(none provided)",
            "plan_steps": formatted_steps,
            "previous_code": previous_code or "(none — this is round 1, write from scratch)",
            "execution_output": trimmed_exec,
        }

        # Parse column names from schema_hints once; the extractor uses
        # these to recover when the model misnames the JSON key.
        known_columns = _parse_columns_from_schema_hints(schema_hints)

        # Lazy chain init: build OUTSIDE the lock (heavy — LLM init, imports),
        # then assign under the lock to ensure exactly-once initialisation even
        # when two coroutines race here simultaneously on the first call.
        if self._chain is None:
            built = self._build_chain()          # heavy work — no lock held
            async with self._lock:               # lock only for the assignment
                if self._chain is None:          # double-checked locking
                    self._chain = built
        chain = self._chain

        resolved_model = self._get_resolved_model()
        is_raw_path = self._is_raw_path()
        logger.info(
            "[Coder] generate_code model=%s mode=%s force_raw=%s known_columns=%d",
            resolved_model,
            "raw" if is_raw_path else "structured",
            self._force_raw,
            len(known_columns),
        )

        if is_raw_path:
            # ── Raw-completion path (CodeLlama / Llama-Instruct / sticky) ──
            # Structured output is unreliable for these models. We invoke the
            # plain LLM chain and extract code from the text response.
            try:
                raw_result = await chain.ainvoke(
                    invoke_input,
                    config=tracker_callback_config(token_tracker),
                )
                raw_text = getattr(raw_result, "content", "") or ""
            except Exception as raw_exc:
                # Hard fallback: rebuild a fresh raw chain with raw-text output
                # instructions (no JSON wrapper), then retry once.
                from core.llm_client import get_nim_llm  # pylint: disable=import-outside-toplevel
                raw_llm_fresh = get_nim_llm(
                    model=resolved_model,
                    temperature=self._temperature,
                    cache_scope="raw",
                    use_cache=False,
                )
                raw_prompt = ChatPromptTemplate.from_messages([
                    ("system", _CODER_SYSTEM),
                    ("human", _CODER_HUMAN_RAW),
                ])
                raw_chain_fresh = raw_prompt | raw_llm_fresh
                try:
                    raw_result = await raw_chain_fresh.ainvoke(
                        invoke_input,
                        config=tracker_callback_config(token_tracker),
                    )
                    raw_text = getattr(raw_result, "content", "") or ""
                    logger.warning(
                        "[Coder] Raw chain failed once (%s); fresh raw fallback succeeded.",
                        type(raw_exc).__name__,
                    )
                except Exception as raw_exc_2:
                    # Detect column-name hallucination in the NIM error and
                    # engage sticky raw mode so subsequent rounds use a clean
                    # chain (even though this call itself cannot recover).
                    if known_columns:
                        _exc_combo = (str(raw_exc) + str(raw_exc_2)).lower()
                        _matched_col = next(
                            (c for c in known_columns
                             if len(c) >= 2 and c.lower() in _exc_combo),
                            None,
                        )
                        if _matched_col is not None:
                            self.force_raw_completion(
                                reason=(
                                    f"NIM API error contains column name "
                                    f"'{_matched_col}'"
                                )
                            )

                    if _is_column_listing_query(query):
                        logger.warning(
                            "[Coder] Raw completion failed twice for column-listing query; "
                            "using deterministic fallback script."
                        )
                        return _build_column_listing_fallback_script(known_columns).strip()

                    if _is_data_distribution_query(query):
                        logger.warning(
                            "[Coder] Raw completion failed twice for distribution query; "
                            "using deterministic fallback script."
                        )
                        return _build_distribution_fallback_script().strip()

                    raise ValueError(
                        "[Coder] Raw completion failed (primary + fresh fallback). "
                        f"Primary: {str(raw_exc)[:180]} | Fallback: {str(raw_exc_2)[:180]}"
                    ) from raw_exc_2
            code = _extract_code_from_model_text(raw_text, known_columns=known_columns)
            if not code.strip():
                raise ValueError(
                    f"[Coder] Raw completion returned empty/unparseable response "
                    f"({len(raw_text)} chars): {raw_text[:200]!r}"
                )
            logger.info("[Coder] Raw-completion path produced %d chars.", len(code))
        else:
            # ── Structured-output path (capable models) ────────────────────
            try:
                result: CodeOutput = await chain.ainvoke(
                    invoke_input,
                    config=tracker_callback_config(token_tracker),
                )
                code = result.code
            except Exception as structured_exc:
                # Structured output failed (schema drift, parser error, etc.).
                # Always fall back to a raw-completion attempt regardless of
                # exception type — the NVIDIA NIM structured-output path is
                # known to raise KeyError/ValidationError when the model emits
                # a data-column name (e.g. "column_name") as the JSON key instead of
                # the required "code" key.
                exc_payload = str(structured_exc)
                logger.warning(
                    "[Coder] Structured output failed (%s); falling back to raw completion.",
                    type(structured_exc).__name__,
                )

                # If the exception text references a known column name as the
                # missing/unexpected key, this is the column-key hallucination
                # — flip into sticky raw-completion mode so subsequent rounds
                # don't keep crashing the same way.
                if known_columns:
                    exc_lower = exc_payload.lower()
                    matched_col = next(
                        (c for c in known_columns if c.lower() in exc_lower),
                        None,
                    )
                    if matched_col is not None:
                        self.force_raw_completion(
                            reason=(
                                f"structured output emitted column-name key "
                                f"'{matched_col}'"
                            )
                        )

                # Attempt 1: try to extract code from the exception payload
                # itself — some parsers embed the raw model text in the message.
                code_from_exc = _extract_code_from_model_text(
                    exc_payload, known_columns=known_columns
                )

                if code_from_exc and len(code_from_exc) > 30:  # non-trivial script
                    code = code_from_exc
                    logger.info(
                        "[Coder] Extracted %d chars from exception payload (skipping raw call).",
                        len(code),
                    )
                else:
                    # Attempt 2: invoke the same cached LLM without structured
                    # output binding so it returns plain text.
                    from core.llm_client import get_nim_llm  # pylint: disable=import-outside-toplevel
                    raw_llm = get_nim_llm(
                        model=resolved_model,
                        temperature=self._temperature,
                        cache_scope="raw",
                        use_cache=False,  # avoid structured/raw cache contamination
                    )
                    fallback_prompt = ChatPromptTemplate.from_messages([
                        ("system", _CODER_SYSTEM),
                        ("human", _CODER_HUMAN_RAW),
                    ])
                    raw_chain = fallback_prompt | raw_llm
                    try:
                        raw_result = await raw_chain.ainvoke(
                            invoke_input,
                            config=tracker_callback_config(token_tracker),
                        )
                        raw_text = getattr(raw_result, "content", "") or ""
                        code = _extract_code_from_model_text(
                            raw_text, known_columns=known_columns
                        )
                        logger.info("[Coder] Raw fallback produced %d chars.", len(code))
                    except Exception as raw_exc:
                        if _is_column_listing_query(query):
                            logger.warning(
                                "[Coder] Structured+raw fallback failed for column-listing query; "
                                "using deterministic fallback script."
                            )
                            return _build_column_listing_fallback_script(known_columns).strip()
                        if _is_data_distribution_query(query):
                            logger.warning(
                                "[Coder] Structured+raw fallback failed for distribution query; "
                                "using deterministic fallback script."
                            )
                            return _build_distribution_fallback_script().strip()
                        raise ValueError(
                            f"[Coder] Structured output + raw fallback both failed. "
                            f"Structured error: {exc_payload[:150]}, "
                            f"Raw error: {str(raw_exc)[:150]}"
                        ) from raw_exc

                    if not code.strip():
                        # Attempt 3: last resort — re-raise so the orchestrator
                        # retry loop can inject the schema hint into the next call.
                        raise ValueError(
                            f"[Coder] Structured output + raw fallback both failed. "
                            f"Original error: {exc_payload[:300]}"
                        ) from structured_exc

        # Strip accidental markdown fences (defensive)
        code = _extract_code_from_model_text(code, known_columns=known_columns)

        logger.info("[Coder] Generated code (%d chars).", len(code))
        return code.strip()
