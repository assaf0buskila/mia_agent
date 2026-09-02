# Mia wiring map

Who calls whom. Living specs stay `PRODUCT.md` / `ARCHITECTURE.md` / `DECISIONS.md`.

```text
Website visitor
  widget.js       app/web/ask_mia.js
       │
       ▼
  HTTP            app/api/website.py
       │          origin-bind
       ▼
  site loop       app/surfaces/site.py
       │          identify-then-sell
       │          CRM + Telegram ping only after phone or email
       ▼
  Contacts CRM    app/surfaces/crm.py
                  spreadsheet 1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI

Assaf (numeric Telegram id only)
  webhook         app/api/telegram.py
       ▼
  owner loop      app/surfaces/owner.py
       ▼
  owner agent     app/graph/owner_agent.py
       │          crm_search / crm_upsert / house Composio reads
       ▼
  house ports     bind_owner_house_ports(settings)
                  Gmail, Sheets, Instagram, LinkedIn, GA, GSC, Calendar
```

Kill-switch does not 503 owner talk or site chat. High-risk writes still go through policy.

WhatsApp customer send stays Assaf. Website visitors cannot run owner tools.
