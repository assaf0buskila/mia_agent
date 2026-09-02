# Mia brain

Living product: `docs/PRODUCT.md`. Loops: `docs/ARCHITECTURE.md`. Memory tables and retrieval constants live in `app/brain/`.

Owner Telegram is a Dude-like tool loop (`app/graph/owner_agent.py`). Website is identify-then-sell (`app/surfaces/site.py`). Shared memory and published-facts knowledge sit under `app/brain/`.

High-risk writes still go through policy. The env kill switch does not 503 owner talk or site chat.

CRM is the locked Contacts + Activity workbook. No invented metrics. No seventh agent.
