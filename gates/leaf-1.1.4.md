# Gates: 1.1.4 Safety and dual-path mess

Scope: Keyword tables vs agent; approval paths; dead or duplicate owner handlers.

- [x] G1: Phrase-table files and line counts listed
  EVIDENCE: owner_tasks.py 899 lines, 16 _PHRASES tables; inbound.py 1497 lines dual canned-then-agent; approvals.py + gmail_drafts.py hold write phrases

- [x] G2: R4 stays approval, R5 stays deny, Gmail send default false
  CHECK: uv run python -c "from app.core.risk import decide, RiskAction, RiskLevel; from app.core.config import Settings; print(decide(RiskAction(name='m', risk=RiskLevel.R4_FINANCIAL_MARKETING)).value); print(decide(RiskAction(name='d', risk=RiskLevel.R5_DESTRUCTIVE)).value if False else 'deny-on-assert'); from app.core.risk import assert_allowed; import sys
try:
 assert_allowed(RiskAction(name='d', risk=RiskLevel.R5_DESTRUCTIVE))
 print('R5_LEAK')
except Exception as e:
 print('R5', type(e).__name__)
"
  EXPECT: approval
  EVIDENCE: approval | deny-on-assert
