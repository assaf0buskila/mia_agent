# ADR-044 Repair strict provider contracts and remove lazy-user handoff friction

- **Status:** accepted
- **Date:** 2026-08-31
- **Assaf:** ADOPT (chat: complete the package; design every flow for the lazy user)

**Context**
The first ADR-043 release advertised a dynamic Composio execute function with a nested arbitrary
object while marking every function schema strict. OpenAI validates the whole advertised tool set
before running the owner turn, so this one open object could return the owner-facing provider error
before Sheets or Composio executed. Separately, an authorized Sheets URL was unusable without
manually copying its ID and typing an A1 range. The website exposed WhatsApp only after selected
conversation actions and its footer handoff required two taps. Telegram voice discarded declared
media metadata, named every upload `note.ogg`, and ignored audio sent as a document.

**Decision**
The generic Composio execute meta-tool accepts `arguments_json` as a strict string, decodes it
locally to an object, then applies the unchanged current provider schema validation and Python risk
policy. A recursive test requires every advertised object schema to be closed. Authorized Sheets
reads accept an exact HTTPS Google Sheets URL, extract the ID locally, apply the existing allowlist,
and use only `A1:J20` on the first visible tab when no range was provided; writes still require an
explicit ID, range, and values. Website config returns only its configured `wa.me` destination and
the existing WhatsApp action becomes a one-tap open plus best-effort idempotent handoff notify after
session creation. Phone text from the model is never linked. Telegram preserves declared audio
metadata, derives the actual transcription filename, safely falls back from generic CDN MIME, and
routes audio documents through the same STT path.

**Consequences**
An unrelated malformed dynamic definition can no longer take down every OwnerGraph request.
Assaf can paste an allowlisted Sheet URL without provider-specific syntax, and a website visitor
always has the shortest configured route to WhatsApp. Incognito does not change server-side
Telegram delivery; it can still expose origin, privacy-extension, or blocked-network failures, so
production acceptance must correlate the browser request with server delivery evidence. `gid` is
not silently mapped to a tab in this slice: a link-only preview reads the first visible tab. The
current `gpt-transcribe` model remains configured because its official model page lists the file
transcriptions endpoint; no model is changed without an authenticated probe.

**Alternatives considered**
Mark the dynamic function non-strict — rejected because it would weaken the uniform schema contract.
Allow arbitrary URLs or Drive search — rejected because a link is not authorization. Automatically
link any phone number in generated text — rejected because the number can be hallucinated or
untrusted. Wait for Telegram before opening WhatsApp — rejected because transport latency should not
block the visitor's tap. Change the STT model from documentation ambiguity alone — rejected; the
current official model page explicitly supports `/v1/audio/transcriptions`, and a live authenticated
probe is the remaining proof.
