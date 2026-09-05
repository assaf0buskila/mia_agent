# DECISIONS — ADR index

Architectural Decision Records for Mia. One decision per file under `docs/adr/`.
Nygard fields: context, decision, consequences.

**Read this index. Do not load every ADR.** Open a record only when the task
touches it. New record: copy [`adr/TEMPLATE.md`](adr/TEMPLATE.md), take the next
free number, add a row here.

Numbers are permanent. ADR-020 was never issued. ADR-033 is reserved and has no
record yet. Older records name files and folders that no longer exist (`docs/archive/`,
`docs/PRD.md`, `docs/BUILD_STATUS.md`, `docs/PROJECT_MAP.md`, `docs/RUNBOOK.md`,
`docs/PRODUCTION_BUILD.md`); those historical build documents live in git history.
Record bodies are kept verbatim rather than rewritten.

## Status words

| Status | Meaning |
| --- | --- |
| proposed | Written; Assaf has not chosen yet |
| accepted | Assaf chose KEEP or ADOPT |
| superseded | Replaced by a later ADR |
| rejected | Assaf chose not to take this path |

Proposed is not accepted. Build may follow a proposed default only when `AGENTS.md`
and the accepted ADRs say to.

## Index

| ADR | Decision | Status | Detail |
| --- | --- | --- | --- |
| ADR-000 | Bible v1.1 is the product baseline | accepted | [record](adr/ADR-000-bible-v1-1-is-the-product-baseline.md) |
| ADR-001 | Repo root is this workspace | proposed | [record](adr/ADR-001-repo-root-is-this-workspace.md) |
| ADR-002 | Phase 0 uses AGENTS.md only | proposed | [record](adr/ADR-002-phase-0-uses-agents-md-only.md) |
| ADR-003 | Finish Phase 0 control docs before pyproject.toml | proposed | [record](adr/ADR-003-finish-phase-0-control-docs-before-pyproject-toml.md) |
| ADR-004 | Keep PRD living while building | accepted | [record](adr/ADR-004-keep-prd-living-while-building.md) |
| ADR-005 | uv with pinned FastAPI and LangGraph | accepted | [record](adr/ADR-005-uv-with-pinned-fastapi-and-langgraph.md) |
| ADR-006 | WhatsApp ingress stays Meta webhook; Composio is not the WhatsApp brain | accepted | [record](adr/ADR-006-whatsapp-ingress-stays-meta-webhook-composio-is-not-the-what.md) |
| ADR-007 | Pick the best adapter per job; do not default to Composio | accepted | [record](adr/ADR-007-pick-the-best-adapter-per-job-do-not-default-to-composio.md) |
| ADR-008 | Today-vs-baseline = previous 7 completed local days' daily average | accepted | [record](adr/ADR-008-today-vs-baseline-previous-7-completed-local-days-daily-aver.md) |
| ADR-009 | Composio LinkedIn profile + direct member post analytics | accepted | [record](adr/ADR-009-composio-linkedin-profile-direct-member-post-analytics.md) |
| ADR-010 | Explicit company domain for meeting research | accepted | [record](adr/ADR-010-explicit-company-domain-for-meeting-research.md) |
| ADR-011 | Calendar create after explicit slot confirmation | accepted | [record](adr/ADR-011-calendar-create-after-explicit-slot-confirmation.md) |
| ADR-012 | Meeting availability policy (Sun–Thu IL business hours) | accepted | [record](adr/ADR-012-meeting-availability-policy-sun-thu-il-business-hours.md) |
| ADR-013 | Automatic confirmed reschedule; cancellation request for Assaf | accepted | [record](adr/ADR-013-automatic-confirmed-reschedule-cancellation-request-for-assa.md) |
| ADR-014 | First AWS production: Fargate + RDS + Secrets Manager box | accepted | [record](adr/ADR-014-first-aws-production-fargate-rds-secrets-manager-box.md) |
| ADR-015 | Production adapter map (Composio vs Meta vs Firecrawl) | accepted | [record](adr/ADR-015-production-adapter-map-composio-vs-meta-vs-firecrawl.md) |
| ADR-016 | WhatsApp inbound stays Meta; Composio may own send | accepted | [record](adr/ADR-016-whatsapp-inbound-stays-meta-composio-may-own-send.md) |
| ADR-017 | v1 communication operating model | accepted | [record](adr/ADR-017-v1-communication-operating-model.md) |
| ADR-018 | Website offers WhatsApp after first real friction | accepted | [record](adr/ADR-018-website-offers-whatsapp-after-first-real-friction.md) |
| ADR-019 | Selected Region is eu-north-1 | accepted | [record](adr/ADR-019-selected-region-is-eu-north-1.md) |
| ADR-021 | Documentation core set; ManyChat not a v1 runtime channel | superseded | [record](adr/ADR-021-documentation-core-set-manychat-not-a-v1-runtime-channel.md) |
| ADR-022 | Production live sales test: leave shadow, keep gated writes off | accepted | [record](adr/ADR-022-production-live-sales-test-leave-shadow-keep-gated-writes-of.md) |
| ADR-023 | Model routing: deterministic decisions, model paraphrases | proposed | [record](adr/ADR-023-model-routing-deterministic-decisions-model-paraphrases.md) |
| ADR-024 | WhatsApp stays human until official Cloud API inbound | accepted | [record](adr/ADR-024-whatsapp-stays-human-until-official-cloud-api-inbound.md) |
| ADR-025 | Conversation reasoning on website and Telegram paraphrasers | accepted | [record](adr/ADR-025-conversation-reasoning-on-website-and-telegram-paraphrasers.md) |
| ADR-026 | Mia's brain: long-term memory, knowledge, and an owner tool loop | accepted | [record](adr/ADR-026-mia-s-brain-long-term-memory-knowledge-and-an-owner-tool-loo.md) |
| ADR-027 | Opt-in Composio discovery for GSC/GA4/Meta; Sheets and LinkedIn stay explicit | accepted | [record](adr/ADR-027-opt-in-composio-discovery-for-gsc-ga4-meta-sheets-and-linked.md) |
| ADR-028 | Visitor knowledge on the website path, answer-then-ask, and the meeting as default exit | accepted | [record](adr/ADR-028-visitor-knowledge-on-the-website-path-answer-then-ask-and-th.md) |
| ADR-029 | Website conversion funnel, engine truth line, and multi-owner notification | accepted | [record](adr/ADR-029-website-conversion-funnel-engine-truth-line-and-multi-owner-.md) |
| ADR-030 | Owner Telegram: free conversation, typed Gmail reads, lead by name | accepted | [record](adr/ADR-030-owner-telegram-free-conversation-typed-gmail-reads-lead-by-n.md) |
| ADR-031 | Owner intent: same agent, no sub-agents | accepted | [record](adr/ADR-031-owner-intent-same-agent-no-sub-agents.md) |
| ADR-032 | Owner reads: wider tool loop, live dates, agenda, deterministic query normalization | accepted | [record](adr/ADR-032-owner-reads-wider-tool-loop-live-dates-agenda-deterministic-.md) |
| ADR-033 | reserved — Gmail send after Approve (in flight on `claude/mia-adr033-wip`, not yet merged) | proposed | reserved — no record |
| ADR-034 | LinkedIn v1 is Composio profile; member-analytics token is optional | accepted | [record](adr/ADR-034-linkedin-v1-is-composio-profile-member-analytics-token-is-op.md) |
| ADR-035 | Apify google-search-scraper behind ResearchPort | accepted | [record](adr/ADR-035-apify-google-search-scraper-behind-researchport.md) |
| ADR-036 | VNext two graphs + canonical docs | accepted | [record](adr/ADR-036-vnext-two-graphs-canonical-docs.md) |
| ADR-037 | Delete ManyChat from the product | accepted | [record](adr/ADR-037-delete-manychat-from-the-product.md) |
| ADR-038 | Graphs own retrieve and conversation complete | accepted | [record](adr/ADR-038-graphs-own-retrieve-and-conversation-complete.md) |
| ADR-039 | Drop Meta ads, LinkedIn post analytics, campaigns, pacing and prelaunch | accepted | [record](adr/ADR-039-drop-meta-ads-linkedin-post-analytics-campaigns-pacing-and-p.md) |
| ADR-040 | Prospect tone awareness in the website sales prompt | accepted | [record](adr/ADR-040-prospect-tone-awareness-in-the-website-sales-prompt.md) |
| ADR-041 | The permission principal is derived from the request | accepted | [record](adr/ADR-041-the-permission-principal-is-derived-from-the-request.md) |
| ADR-042 | Authorized Sheets updates and normalized AssafWeb KPI reads | accepted | [record](adr/ADR-042-authorized-sheets-updates-and-normalized-assafweb-kpi-reads.md) |
| ADR-043 | Owner-only on-demand Composio tool breadth | accepted | [record](adr/ADR-043-owner-only-on-demand-composio-tool-breadth.md) |
| ADR-044 | Repair strict provider contracts and remove lazy-user handoff friction | accepted | [record](adr/ADR-044-repair-strict-provider-contracts-and-remove-lazy-user-handof.md) |
| ADR-045 | Complete-work owner actions and Mia-managed CRM workspace | accepted | [record](adr/ADR-045-complete-work-owner-actions-and-mia-managed-crm-workspace.md) |
| ADR-046 | Official Composio destructive slugs stay denied; WhatsApp-move ping is a summary | accepted | [record](adr/ADR-046-official-composio-destructive-slugs-stay-denied-whatsapp-mov.md) |
| ADR-047 | Owner-requested Gmail send stays; unsolicited send and delete-forever stay denied | accepted | [record](adr/ADR-047-owner-requested-gmail-send-stays-unsolicited-send-and-delete.md) |
| ADR-048 | Rebuild Mia as a Dude clone with Contacts/Activity CRM | accepted | [record](adr/ADR-048-rebuild-mia-as-a-dude-clone-with-contacts-activity-crm.md) |
| ADR-049 | SITE Mia answers first; contact only for Assaf or Sheet | accepted | [record](adr/ADR-049-site-mia-answers-first-contact-only-for-assaf-or-sheet.md) |
| ADR-050 | Two-state tools and Tel Aviv calendar write gate | accepted | [record](adr/ADR-050-two-state-tools-and-tel-aviv-calendar-write-gate.md) |
| ADR-051 | Visitor replies never name tools; owner STT/images/Sheets aliases | accepted | [record](adr/ADR-051-visitor-replies-never-name-tools-owner-stt-images-sheets-ali.md) |
| ADR-052 | Delete the dead Sheets mirror and the unread meeting-first flag | accepted | [record](adr/ADR-052-delete-the-dead-sheets-mirror-and-the-unread-meeting-first-f.md) |
| ADR-053 | Site Mia sells on a ladder; WhatsApp waits on Baileys | accepted | [record](adr/ADR-053-site-mia-sells-on-a-ladder-whatsapp-waits-on-baileys.md) |
| NOTE | Gemini sales fallback (Assaf 2026-08-22) | accepted | [record](adr/NOTE-gemini-sales-fallback-assaf-2026-08-22.md) |

`NOTE` is an accepted decision recorded without an ADR number. It still binds.
