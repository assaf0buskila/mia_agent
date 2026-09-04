# Phase 1.7 local release evidence

Date: 2026-08-31

Scope: direct website sales/human handoff, truthful Telegram owner-notification
delivery state, and owner-only on-demand Composio tool discovery.

## Implemented locally

- Direct Hebrew/English requests to reach Assaf or buy enter HANDOFF immediately;
  the reproduced `אפשר להגיע לאסף?` -> `יאללה` path cannot fall back to discovery.
- ClientGraph owns the deterministic HANDOFF visitor response. Model prose cannot
  independently advertise a completed transfer.
- Per-recipient owner-notification rows persist `pending`, `accepted`, or legacy
  delivery state. Accepted replay is truthful without resending, ambiguous replay is
  fail-closed without resending, and definite rejection releases only that recipient
  for retry.
- OwnerGraph has three bounded Composio meta-tools for searching ACTIVE connected
  toolkits, loading one exact schema, and executing a schema-preflighted deterministic
  read recognized by the conservative classifier. Client principals are denied before
  adapter construction. Unfamiliar actions fail closed; generic side-effect
  tools remain closed until they have a named approval, idempotency, and audit contract.

## Final-tree mechanical evidence

- `uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp
  .pytest-tmp/root-phase17-true-final -q`: PASS, 2,623 collected tests.
- `uv --offline --cache-dir .uv-cache run ruff check app tests`: PASS.
- `uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py`: PASS,
  273/273 (sales 51, buyer 43, calendar 20, website handoff 15, safety 20,
  objection 20, routing 20, extract 30, writing 33, gold 21).
- `git diff --check`: PASS; Git emitted only repository line-ending notices.
- Fresh HEAVY review: RELEASE PASS with no remaining P0-P2 blocker.

## Explicit non-claims

- No commit, push, image build, migration application, deployment, production secret
  inspection, live provider execution, or visitor/owner message was performed.
- Production needs the additive recipient-delivery-state migration and an exact-SHA
  deployment before this behavior exists live.
- Controlled browser-to-Telegram acceptance, post-deploy provider reads, live Telegram
  voice, and any separately approved provider write remain open.
