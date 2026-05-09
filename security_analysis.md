# Security Analysis: Agentloop (DS-STAR Platform)

## Overview
This document contains a prioritized security audit of the Agentloop codebase. The audit focuses on network configuration, execution environments, authentication, input validation, database access controls, and dependency risks.

## Prioritized Findings

| # | Issue | Location | Severity | Fix Summary |
|---|---|---|---|---|
| 1 | **Remote Code Execution (RCE) via Subprocess Fallback** | `backend/core/executor/code_executor.py` | **CRITICAL** | Remove the automatic fallback from Docker to local OS `subprocess`. If `DOCKER_SANDBOX_ENABLED=true` and Docker fails, raise an exception (fail-closed) rather than executing untrusted LLM-generated code on the host OS. Enforce `gVisor` or strict `seccomp` profiles in Docker. |
| 2 | **Permissive CORS Configuration (`allow_origins=["*"]`)** | `backend/main.py` | **HIGH** | The API allows cross-origin requests from any domain by defaulting `ALLOWED_ORIGINS` to `*`. This permits CSRF attacks and session hijacking. Restrict `ALLOWED_ORIGINS` to the exact frontend domain (e.g., `["https://app.agentloop.ai"]`). |
| 3 | **Unbounded Query Input Length (DoS & Billing Risk)** | `backend/models/schemas.py` & `backend/api/routes.py` | **HIGH** | `AgentRunRequest.query` is an unconstrained string. Attackers can submit multi-megabyte strings, causing memory exhaustion or massive LLM API bills (token exhaustion). Add `Field(..., max_length=5000)` to the Pydantic model. |
| 4 | **JWT Algorithm Confusion & Missing Claims Validation** | `backend/middleware/auth.py` | **HIGH** | `verify_aud: False` is passed to PyJWT, meaning audience claims are ignored. Furthermore, the flexible fallback between ES256 and HS256 could allow algorithm confusion attacks if an attacker signs a token using the ES256 public key as an HS256 secret. Explicitly lock the required algorithm based on configuration and enforce audience verification. |
| 5 | **RLS Bypass via Service Role Client Writes** | `backend/services/supabase_service.py` | **MEDIUM** | Functions like `create_agent_run` and `insert_file_record` use `get_service_role_client()`, which entirely bypasses Supabase Row-Level Security (RLS). Tenant isolation rests solely on FastAPI logic. Migrate to instantiating a user-scoped Supabase client using the provided JWT access token for writes. |
| 6 | **Cross-Session Leakage Risk via Optional Auth** | `backend/api/routes.py` | **MEDIUM** | `session_key = request.session_id or str(auth.user_id)`. If an attacker explicitly provides another user's `session_id` in the query params, they might hijack their in-memory file cache in `_session_contexts`. Bind session caches strictly to `auth.user_id` on protected routes. |
| 7 | **Dependency Version Pinning** | `backend/requirements.txt` & `package.json` | **LOW** | Dependencies use `>=` bounds instead of exact pinned versions (e.g., `==`). This creates risks of supply chain attacks or broken builds if a compromised or breaking transitive dependency is released. Use `pip-compile` and `package-lock.json` rigorously. |

---

## Detailed Remediation Steps

### 1. Hardening CORS
**Vulnerable Code** (`backend/main.py`):
```python
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
```

**Corrected Code**:
```python
allowed_origins_env = os.getenv("ALLOWED_ORIGINS")
if not allowed_origins_env:
    raise ValueError("ALLOWED_ORIGINS must be explicitly set in production.")
allowed_origins = [origin.strip() for origin in allowed_origins_env.split(",")]
```

### 2. Sandbox RCE Fallback Prevention
**Vulnerable Code** (`backend/core/executor/code_executor.py`):
```python
        except Exception as exc: 
            logger.warning("[Executor] Docker unavailable (%s) — falling back to subprocess.", exc)
            return self._run_sync(code, session_id=session_id)
```

**Corrected Code**:
```python
        except Exception as exc: 
            logger.error("[Executor] Docker unavailable (%s). Halting execution to prevent RCE.", exc)
            raise RuntimeError("Sandboxed execution environment unavailable.") from exc
```

### 3. Securing JWT Validation
**Vulnerable Code** (`backend/middleware/auth.py`):
```python
            payload = jwt.decode(
                token,
                public_key,
                algorithms=[alg],
                options={"verify_aud": False},
            )
```

**Corrected Code**:
```python
            payload = jwt.decode(
                token,
                public_key,
                algorithms=["ES256"], # Strictly lock algorithms
                audience="authenticated",
                options={"verify_aud": True},
            )
```

### 4. Enforcing Pydantic Input Constraints
**Vulnerable Code** (`backend/models/schemas.py`):
```python
class AgentRunRequest(BaseModel):
    query: str
```

**Corrected Code**:
```python
from pydantic import BaseModel, Field

class AgentRunRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000, description="The user's query.")
```
