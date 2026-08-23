# Handoff

**Date:** 2026-08-23  
**Load:** `docs/PROJECT_MAP.md` → `AGENTS.md` → `docs/ARCHITECTURE.md` → `docs/DECISIONS.md` → current tree.

Package manager **uv**. Python `>=3.12`. PowerShell: `;` not `&&`. Do not inspect `.env`. Production keys: AWS Secrets Manager `mia/prod`.

## Current

v1 channels (ADR-017) are in code. Live Fargate is **mia:15** (task `mia:16`; ADR-022: `auto_approved` for verified WhatsApp handoff). Customer-facing Hebrew (widget pill, composer, canned sales, WhatsApp paste lines) is 2nd-person plural / impersonal so it addresses men and women; owner Telegram stays masculine. Sales prompt pin is `sales_reply_v6`. Telegram unclassified **text** returns an owner status digest only for greetings, status pings and text of three words or fewer; anything longer gets an Understanding Check. Widget chrome is pinned to the scraped `www.assafweb.com` `:root` tokens, ChatBubble-style rows, and opens only on click. ManyChat route is unmounted.

## Do not redo

Adapter map ADR-015, WhatsApp send split ADR-016, first AWS host ADR-014, Region eu-north-1 ADR-019.

## Next

1. Assaf live-tests website → WhatsApp handoff. Unknown WhatsApp stays silent.
2. Keep Gmail send / Meta writes / IG auto-reply off. Rollback = `mia:9` + `shadow`.

Historical slice notes: `docs/archive/HANDOFF.md`.
