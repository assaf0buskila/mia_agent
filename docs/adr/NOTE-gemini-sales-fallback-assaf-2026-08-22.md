# Gemini sales fallback (Assaf 2026-08-22)

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** add Google supplier fallback for sales paraphrase

**Decision**
Keep OpenAI as the primary sales Chat Completions path. After OpenAI primary + optional OpenAI fallback model, try Gemini AI Studio once via the official OpenAI-compat URL `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions` (`MIA_GEMINI_API_KEY` + `MIA_SALES_GEMINI_MODEL`). Same Human Voice lint; fail → canned. Not Vertex. Not a Gmail/Calendar path. Model ids stay env/eval (ADR-007).

**Alternatives considered**
Vertex AI OpenAI-compat — rejected for local operator setup (GCP project + ADC). Native Gemini generateContent — rejected this slice; Chat Completions JSON already matches the OpenAI adapter.
