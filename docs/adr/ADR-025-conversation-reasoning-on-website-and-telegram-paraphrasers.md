# ADR-025 Conversation reasoning on website and Telegram paraphrasers

- **Status:** accepted
- **Date:** 2026-08-23
- **Assaf:** ADOPT (chat: give Mia reasoning on conversion, both website and Telegram)

**Context**
Website sales already paraphrased canned copy, but the prompt did not require a think-then-speak step, so the model could parrot or restart. Telegram owner replies were still canned templates. Assaf asked for conversation reasoning on both channels. Dumping the Composio catalog into the Telegram model would let untrusted text pick privileged tools.

**Decision**
Keep next-action, owner-task classification, approvals and Composio calls in Python. Upgrade `sales_reply_v5` so the website model reasons silently about what the prospect said, what is known, and the one conversion move that serves INTENT, then writes only the customer message. Wire `owner_telegram_v2` as a paraphraser over the typed RESULT. If the owner paraphrase drops a lead id or email, looks like a tool call, or the kill switch is on, send the canned RESULT.

**Consequences**
Both channels can sound like a conversation without the model choosing strategy or tools. A model outage still degrades phrasing only. Owner list accuracy is protected by the fact-preservation fallback. No new env knobs; owner phrasing reuses the sales model chain.

**Alternatives considered**
Let the Telegram model pick Composio tools — rejected; catalog dump and untrusted-text tool choice. Free-author sales replies without INTENT — rejected; NBA stays testable in code. Keep Telegram canned — rejected; Assaf asked for reasoning on that channel too.
