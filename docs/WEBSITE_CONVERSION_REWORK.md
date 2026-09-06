# Website conversion rework

Source: user-supplied `MIA_WEBSITE_CONVERSION_REWORK_PLAN.md`, 2026-09-06.
Scope: website conversion, widget contact and voice, attribution, durable session state,
and conversion evaluation. Keep Owner Mia, ClientGraph and production adapters intact.

## Execution

- Clean starting checkout; branch `codex/website-conversion-rework`.
- Backend worker: conversion state, policy, phrasing, identity, API persistence/retrieval.
- Widget worker: inline contact, immediate WhatsApp CTA, session lifecycle, recorder.
- Integration: per-turn eval evidence and rejection tests, full local checks, docs.
- Fresh independent reviewer: correctness, safety, simplification, test coverage.

## Acceptance evidence

Verified in the local working tree on 2026-09-06:

| Check | Result |
| --- | --- |
| `uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-tmp-full --tb=line --disable-warnings` | 2951 passed, 1937 warnings, 98.95 seconds |
| `uv --offline --cache-dir .uv-cache run ruff check app tests` | Passed |
| `uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py` | Passed |
| `node tests/unit/widget_behavior.test.js` | Passed; also runs within pytest |
| `git -c core.safecrlf=false diff --check` | Passed |
| Real-model predeploy runner | Skipped: no opt-in and no callable models in the process environment |

The executable widget harness runs the shipped JavaScript against a fake DOM and
transport. It tests structured contact, rejected-contact correction, 404 retry,
immediate WhatsApp CTA, invalid/missing URL status, active/stale/completed sessions,
and a send queued during delayed handoff. It drives microphone controls through the
actual recorder callbacks and checks real Blob bytes uploaded with MP4/WebM filenames,
periodic chunks, guarded fallback, track shutdown and no-chunks rejection.

Backend acceptance covers the nail and strong-first-message flows, model attempts to
reopen discovery, student non-leads versus education businesses, published facts,
safe attribution, and durable contact/handoff/closure across process restarts. The
normal repeated-contact path writes one CRM contact and sends one owner notification;
failed close notifications remain retryable. Tests use fake integration ports and
SQLite. They do not prove distributed exactly-once CRM delivery across worker races
or a crash between the external write and saving its completion flag.

All 20 website evaluation scenarios run offline in regression tests. Structural checks
pass, while the three conversion scenarios correctly fail their live-model proof
requirements when no model is supplied. Every conversion reply is checked, including
visible value and repeated discovery; action labels or billed-but-rejected responses
cannot establish acceptance. The real-model nail scenario requires 3/3 successful runs.

Two independent reviewers checked backend and widget/evaluation paths. Their findings
were repaired and reviewed again: session rehydration, failed notification retry,
non-lead classification, handoff queue behavior, contact URL failures and evaluation
false-green cases. Product questions are answered without closing business/friction
discovery; Hebrew and English neutral follow-ups prove that lookup alone does not
trigger contact capture. Their final bounded reviews are recorded in the task conversation.

Local mocks do not prove paid-model quality, live CRM/Telegram, physical iPhone recording,
production behavior, or deployment. No `.env` contents were inspected. The supplied
document's release checklist is not a deployment instruction; no merge, production
send or deploy was performed.

## Required release checks

- Full pytest and Ruff; website origin binding and durable contact/ping coverage.
- Executable widget checks for structured contact, WhatsApp visibility and voice bytes.
- Real-model conversion gate: every reply retained; exact nail scenario passes 3/3.
- Physical iPhone Safari and desktop Chrome Hebrew voice: nonempty transcript.
- Authorized production smoke and manual contact flow after a separately requested release.
