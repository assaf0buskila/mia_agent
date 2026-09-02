# Mia wiring map

Who calls whom. Living specs: `PRODUCT.md` / `ARCHITECTURE.md` / `DECISIONS.md`.

```text
Website visitor
  widget.js       app/web/ask_mia.js
                  glass Hebrew RTL
       │
       ▼
  HTTP            app/api/website.py
                  origin-bind
       ▼
  site loop       app/surfaces/site.py
                  identify-then-sell
                  CRM + Telegram ping only after phone or email
       ▼
  Contacts CRM    app/surfaces/crm.py
                  spreadsheet 1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI
                  Contacts + Activity only

Assaf (numeric Telegram id only)
  webhook         app/api/telegram.py
       ▼
  owner loop      app/surfaces/owner.py
                  Dude talk
       ▼
  owner agent     app/graph/owner_agent.py
                  crm_search / crm_upsert / house Composio reads
       ▼
  house ports     bind_owner_house_ports(settings)
                  Sheets, Gmail, Instagram, LinkedIn, GA, GSC, Calendar
```

WhatsApp customer send stays Assaf. Website visitors cannot run owner tools. No invented metrics.
