# ADR-005 uv with pinned FastAPI and LangGraph

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** unset (implementation default; Assaf may change)

**Context**
Official LangGraph install shows pip and uv. Official FastAPI docs require pinning the FastAPI minor version and not pinning Starlette. Local Python is 3.14.3. PyPI on 21 Aug 2026: FastAPI 0.141.1, LangGraph 1.2.11, pydantic-settings 2.15.0.

**Decision**
Use uv. `requires-python = ">=3.12"`. Pin `fastapi[standard]>=0.141.1,<0.142.0`, `langgraph>=1.2.11,<2`, `pydantic-settings>=2.15.0,<3`. Do not add Composio, AWS, or channel SDKs until that adapter is built.

**Consequences**
Reproducible installs via `uv.lock`. Channel packages land with their adapter, not on day one.

**Alternatives considered**
Poetry / pip-tools — extra tool. Unpinned FastAPI — official docs warn against it. Dumping every integration into pyproject now — dead weight.
