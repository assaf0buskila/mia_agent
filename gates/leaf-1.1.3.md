# Gates: 1.1.3 Customer channels

Scope: Website widget, WhatsApp, Instagram — what the customer graph can and cannot call.

- [x] G1: Confirm customer graph has zero Composio tools
  CHECK: uv run python -c "from pathlib import Path; hits=[p.name for p in (Path('app/graph/orchestrator.py'), Path('app/api/website.py')) if 'composio' in p.read_text(encoding='utf-8').lower()]; print('composio_in', hits or 'none')"
  EXPECT: composio_in none
  EVIDENCE: composio_in none

- [x] G2: WhatsApp send flag default and Instagram auto-reply default named from config
  EVIDENCE: config.py whatsapp_handoff_send=False auto_reply_instagram=False gmail_send=False; live /health confirms those three false; calendar_write true live
