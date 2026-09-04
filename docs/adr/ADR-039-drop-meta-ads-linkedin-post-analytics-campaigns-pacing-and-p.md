# ADR-039 Drop Meta ads, LinkedIn post analytics, campaigns, pacing and prelaunch

- **Status:** accepted
- **Date:** 2026-08-26
- **Assaf:** ADOPT (chat: "Drop them — accept the deletion")

**Context**
A Phase L cleanup pass removed five capability modules — `app/integrations/meta_ads.py`, `app/integrations/linkedin_analytics.py`, `app/domain/campaigns.py`, `app/domain/pacing.py`, `app/domain/prelaunch.py` — plus ~4,275 lines of their tests. They were shipped in image `mia:20` and advertised to the owner agent as the `ads_snapshot` tool and the analytics half of `linkedin_snapshot`, but they were **dormant in production**: `MIA_META_ADS_ACCOUNT_ID` and `MIA_LINKEDIN_ACCESS_TOKEN` are both on the live `/health` missing list and the campaign env vars are blank. Meta member analytics also has no Composio tool (ADR-009, ADR-034), so that half was structurally dark.

**Decision**
Drop all five, with their tests and their wiring: the `ANALYTICS` owner-task branch, the prelaunch gate, the `ads_snapshot` tool, the analytics enrichment inside `linkedin_snapshot`, the campaign Sheets mirror tab, the campaign eval in `app/evals/harness.py`, and the freshness/failure-policy pins that named them. `linkedin_snapshot` survives as a **profile-only** read and its description must stop promising post analytics. The four `app/core/capabilities.py` entries move to `SPECIFIED` with an empty port.

**Consequences**
Mia can no longer answer "how is the campaign spend" or report LinkedIn post reach. Meta Ads and LinkedIn analytics leave the product surface until they are deliberately rebuilt behind the capability layer (§35: capability → policy → adapter → allowlist → tests). Roughly 10k lines leave the repo, which is the first real movement toward §39's "meaningfully smaller". Everything is recoverable from git — the pre-deletion state is `c35d005` and the shipped state is `claude/mia-product-feedback-0bfc90` (`7433abf`).

**Explicitly NOT dropped in the same pass**
The cleanup also removed the Composio WhatsApp outbound sender (`ComposioWhatsAppPort`, `MIA_WHATSAPP_SENDER`) and rewrote `.env.example` to cite **ADR-016** as justification for Meta-only outbound — the opposite of what ADR-016 decides, and contrary to production, which runs `MIA_WHATSAPP_SENDER=composio`. That removal was **rejected and reverted**; ADR-016 stands unchanged. A cleanup pass is not the place to reverse an accepted ADR.

**Alternatives considered**
Keep everything — rejected; dormant, unconfigured integrations are exactly the dead weight §36 warns against, and their 4,275 test lines slowed every run. Keep Meta, drop LinkedIn — considered; rejected because Meta ads is the larger surface (813 lines plus campaigns and pacing) and nothing needs it until Assaf actually runs paid campaigns through Mia. Leave them dark but present — rejected; the tools stayed advertised to the model, so Mia offered a capability that could not work.
