# Gates: Safe live probes and deployment evidence

Scope: Prove the requested production-facing capabilities without secret disclosure or unsafe writes.

- [ ] G1: Telegram voice note is accepted by the live bot and yields a correct text answer from OwnerGraph.
  EVIDENCE: PENDING LIVE OWNER ACTION. On the third refresh, independent and parent checks both observed HTTP 200 status=ok from /health/live, /health/ready, and /health; sanitized health reports Telegram owner enabled and voice input ready with no missing setting names. No real Telegram voice note has yet exercised the deployed bot path, so configuration readiness is not counted as capability proof.
- [ ] G2: Live bounded Sheets read/write proof plus read-only Search Console, GA4, and LinkedIn probes produce classified evidence.
  EVIDENCE: PENDING LIVE OWNER ACTION AND QA-RANGE PREAUTHORIZATION. Sanitized /health now reports Composio, Sheets mirror, LinkedIn profile, Search Console, and GA4 configured with no missing setting names, but no real provider call or Sheet mutation has been attempted. The prepared runbook requires owner Telegram requests plus one explicit RAW QA marker in a privately preauthorized allowlisted range and forbids arbitrary discovery/formulas/clear/delete.
- [ ] G3: Any deployment uses the approved AWS region and secret injection path, with rollback identity recorded.
  EVIDENCE: PREPARED, NOT DEPLOYED. Assaf authorized AWS login; sanitized read-only inspection in eu-north-1 proves service mia ACTIVE on task mia:28, desired/running 1/1, pending 0, primary rollout COMPLETED, with circuit breaker and rollback enabled. `mia:28` is recorded as rollback target. The deployed image is the old HEAD tag and does not contain the intentional worktree changes. No task registration, migration, service update, or secret-value read occurred; this gate remains unchecked until the verified release is actually deployed and stable.
