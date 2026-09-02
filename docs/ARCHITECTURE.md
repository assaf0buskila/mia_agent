# Mia architecture

Two simple loops, shared core, thin channels. Decisions: `docs/DECISIONS.md`. Product: `docs/PRODUCT.md`.

```text
                   MIA
                    │
          ┌─────────┴──────────┐
          │                    │
     OWNER LOOP            SITE LOOP
     Telegram              Website
     text + voice          text + voice
          │                    │
          └─────────┬──────────┘
                    │
            Shared Mia Core
            (identity, STT, Contacts CRM)
                    │
                Composio / direct ports
```

## LangGraph verdict

OwnerGraph and ClientGraph were sequential ceremony around work that already lived in `run_owner_agent` and the website NBA. Runtime Telegram and website paths are simple loops (`app/surfaces/owner.py`, `app/surfaces/site.py`). The old graphs remain in the repo for leftover unit tests. Inner `run_owner_agent` is a real tool loop (not LangGraph) and is reused when a model is configured.

## Surfaces

| Surface | Users | Must not |
| --- | --- | --- |
| Owner loop | Assaf on Telegram | Invent lead IDs; write `01 Leads`; stack a kill-switch that blocks talk |
| Site loop | Website visitors | Write CRM or offer WhatsApp without phone or email; invent prices; run owner tools |

## Channels

Channels adapt transport. They do not reason.

| Surface | Adapter job | Then |
| --- | --- | --- |
| Telegram | Webhook, numeric allowlist, HTML, voice download, callbacks | `run_owner_loop` |
| Website | Session/message/voice/handoff HTTP + widget | `run_site_turn` |
| WhatsApp | Human inbox (ADR-024) | Assaf, after a site ping |

Telegram access: `MIA_TELEGRAM_OWNER_USER_IDS` numeric only. Webhook secret check stays.

## CRM

Locked spreadsheet `1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI`. Tabs Contacts and Activity only. Writers live in `app/surfaces/crm.py`. Owner tools `crm_search` / `crm_upsert` always use that ID. `MIA_SHEETS_SPREADSHEET_ID` overrides if set; empty env still resolves to the locked ID. Live Composio `upsert_lead` is a no-op. Telegram binds house Composio ports from `MIA_COMPOSIO_USER_ID` — if `/health` says connected, tools must run.

## Website UX

Glass Hebrew widget `app/web/ask_mia.js` (`/v1/website/widget.js`). WhatsApp is shown only after a reply that already has phone or email. Config never pre-shows it. Origin-bind stays.

## Runtime

ECS Fargate + RDS + Secrets Manager `mia/prod` + ALB `https://mia.assafweb.com` in **eu-north-1**. Do not copy `.env` onto Fargate. Do not auto-deploy from a rebuild.
