# Mia architecture

Two simple loops, shared core, thin channels. Product: `docs/PRODUCT.md`. Decisions: `docs/DECISIONS.md`.

```text
                   MIA
                    │
          ┌─────────┴──────────┐
          │                    │
     OWNER LOOP            SITE LOOP
     Telegram              Website
     Dude talk             glass widget.js
     text + voice          identify-then-sell
          │                    │
          └─────────┬──────────┘
                    │
            Shared Mia Core
            (identity, STT, Contacts CRM)
                    │
                House Composio
```

## Surfaces

| Surface | Users | Must not |
| --- | --- | --- |
| Owner loop | Assaf on Telegram | Invent metrics; invent lead IDs; ask for a Sheet URL |
| Site loop | Website visitors | Write CRM or offer WhatsApp without phone or email; invent prices; run owner tools |

Runtime paths are `app/surfaces/owner.py` and `app/surfaces/site.py`. The owner tool loop is `app/graph/owner_agent.py`. Leftover LangGraph files stay in-repo for old unit tests only.

## Channels

Channels adapt transport. They do not reason.

| Surface | Adapter job | Then |
| --- | --- | --- |
| Telegram | Webhook, numeric allowlist, HTML, voice download, callbacks | `run_owner_loop` |
| Website | Session/message/voice/handoff HTTP + glass `widget.js` | `run_site_turn` + `site_policy` |
| WhatsApp | Human inbox (ADR-024) | Assaf, after a site ping |

Telegram access: `MIA_TELEGRAM_OWNER_USER_IDS` numeric only. Webhook secret check stays.

## CRM

Locked spreadsheet `1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI`. Live tabs Contacts and Activity only. Archive tabs are gone. Writers live in `app/surfaces/crm.py`. Owner tools `crm_search` / `crm_upsert` always use that ID. Empty `MIA_SHEETS_SPREADSHEET_ID` still resolves to the locked ID. Leftover Composio mirrors are no-ops. Telegram binds house Composio from `MIA_COMPOSIO_USER_ID` — if `/health` says connected, tools must run.

## Website UX

Glass Hebrew widget `app/web/ask_mia.js` at `https://mia.assafweb.com/v1/website/widget.js`. Answer first. WhatsApp only after phone or email. Origin-bind stays.

## Runtime

ECS Fargate + RDS + Secrets Manager `mia/prod` + ALB `https://mia.assafweb.com` in **eu-north-1**. Do not copy `.env` onto Fargate. Do not auto-deploy from a rebuild.
