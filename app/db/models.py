from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CustomerRow(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    identities: Mapped[list["ChannelIdentityRow"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )
    leads: Mapped[list["LeadRow"]] = relationship(back_populates="customer")


class ChannelIdentityRow(Base):
    __tablename__ = "channel_identities"
    __table_args__ = (UniqueConstraint("channel", "external_id", name="uq_channel_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Unused leftover columns (ManyChat removed, ADR-037). Do not write. Keep for existing DBs.
    manychat_subscriber_id: Mapped[str] = mapped_column(String(255), default="")
    manychat_conversation_id: Mapped[str] = mapped_column(String(255), default="")
    customer: Mapped[CustomerRow] = relationship(back_populates="identities")


class LeadRow(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32), default="open")
    conversation_killed: Mapped[bool] = mapped_column(Boolean, default=False)
    human_takeover: Mapped[bool] = mapped_column(Boolean, default=False)
    takeover_state: Mapped[str] = mapped_column(String(32), default="mia_active")
    customer: Mapped[CustomerRow] = relationship(back_populates="leads")
    sales_state: Mapped["SalesStateRow | None"] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )


class SalesStateRow(Base):
    __tablename__ = "lead_sales_state"

    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), primary_key=True)
    pain_level: Mapped[int] = mapped_column(Integer, default=0)
    fit: Mapped[str] = mapped_column(String(16), default="unknown")
    workflow_known: Mapped[bool] = mapped_column(Boolean, default=False)
    impact_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    reflected: Mapped[bool] = mapped_column(Boolean, default=False)
    hypothesis_offered: Mapped[bool] = mapped_column(Boolean, default=False)
    buying_reality_known: Mapped[bool] = mapped_column(Boolean, default=False)
    authority_known: Mapped[bool] = mapped_column(Boolean, default=False)
    timeline_known: Mapped[bool] = mapped_column(Boolean, default=False)
    metric_known: Mapped[bool] = mapped_column(Boolean, default=False)
    willingness_to_meet: Mapped[str | None] = mapped_column(String(8), nullable=True)
    owner_required: Mapped[bool] = mapped_column(Boolean, default=False)
    active_objection: Mapped[str | None] = mapped_column(String(32), nullable=True)
    missing_fields: Mapped[str] = mapped_column(Text, default="[]")
    company_domain: Mapped[str] = mapped_column(String(253), default="")
    whatsapp_handoff_offered: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_step_known: Mapped[bool] = mapped_column(Boolean, default=False)
    data_source_known: Mapped[bool] = mapped_column(Boolean, default=False)
    discovery_turns: Mapped[int] = mapped_column(Integer, default=0)
    asked_actions: Mapped[str] = mapped_column(Text, default="[]")
    explicit_buying_intent: Mapped[bool] = mapped_column(Boolean, default=False)
    headline: Mapped[str] = mapped_column(String(120), default="")
    display_name: Mapped[str] = mapped_column(String(80), default="")
    meeting_exit_offered: Mapped[bool] = mapped_column(Boolean, default=False)
    lead: Mapped[LeadRow] = relationship(back_populates="sales_state")


class OwnerInstructionRow(Base):
    __tablename__ = "owner_instructions"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_owner_instruction_provider_event",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_event_id: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="proposed")


class OwnerCorrectionRow(Base):
    __tablename__ = "owner_corrections"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_owner_correction_provider_event",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_event_id: Mapped[str] = mapped_column(String(255), index=True)
    scope: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="logged")


class FollowUpRow(Base):
    __tablename__ = "lead_follow_ups"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_lead_follow_up"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True, unique=True)
    channel: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    due_at: Mapped[str] = mapped_column(String(10))
    send_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[str] = mapped_column(String(32), default="")
    draft: Mapped[str] = mapped_column(String(500), default="")


class SeoRecommendationRow(Base):
    __tablename__ = "seo_recommendations"
    __table_args__ = (UniqueConstraint("scope", name="uq_seo_recommendation_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(32), default="site", unique=True)
    problem: Mapped[str] = mapped_column(String(255), default="")
    evidence: Mapped[str] = mapped_column(String(255), default="")
    why: Mapped[str] = mapped_column(String(255), default="")
    change: Mapped[str] = mapped_column(String(255), default="")
    metric: Mapped[str] = mapped_column(String(255), default="")


class ContentInsightRow(Base):
    __tablename__ = "content_insights"
    __table_args__ = (UniqueConstraint("media_id", name="uq_content_insight_media"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    media_type: Mapped[str] = mapped_column(String(32))
    views: Mapped[str] = mapped_column(String(32), default="")
    reach: Mapped[str] = mapped_column(String(32), default="")
    likes: Mapped[str] = mapped_column(String(32), default="")
    comments: Mapped[str] = mapped_column(String(32), default="")
    saved: Mapped[str] = mapped_column(String(32), default="")
    lead_signals: Mapped[int] = mapped_column(Integer, default=0)


class DealRow(Base):
    __tablename__ = "deals"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_deal_lead"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True, unique=True)
    stage: Mapped[str] = mapped_column(String(32))
    expected_value: Mapped[str] = mapped_column(String(32), default="")
    closed_value: Mapped[str] = mapped_column(String(32), default="")
    source: Mapped[str] = mapped_column(String(32))
    attribution_confidence: Mapped[str] = mapped_column(String(32))


class MeetingBriefRow(Base):
    __tablename__ = "meeting_briefs"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_meeting_brief"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True, unique=True)
    channel: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text)


class MeetingRow(Base):
    __tablename__ = "meetings"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_meeting_lead"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True, unique=True)
    status: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    scheduled_at: Mapped[str] = mapped_column(String(32), default="")
    calendar_event_id: Mapped[str] = mapped_column(String(1024), default="")
    summary: Mapped[str] = mapped_column(String(32), default="")
    offered_slots_json: Mapped[str] = mapped_column(Text, default="[]")
    meet_link: Mapped[str] = mapped_column(String(512), default="")
    meeting_type: Mapped[str] = mapped_column(String(32), default="intro_call")
    booked_at: Mapped[str] = mapped_column(String(32), default="")
    reschedule_slots_json: Mapped[str] = mapped_column(Text, default="[]")
    rescheduled_at: Mapped[str] = mapped_column(String(32), default="")
    cancellation_requested_at: Mapped[str] = mapped_column(String(32), default="")


class MeetingDebriefRow(Base):
    __tablename__ = "meeting_debriefs"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_meeting_debrief_lead"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True, unique=True)
    outcome: Mapped[str] = mapped_column(String(32))
    next_step: Mapped[str] = mapped_column(String(32), default="none")
    estimated_value: Mapped[str] = mapped_column(String(32), default="")
    notes: Mapped[str] = mapped_column(Text, default="")


class LeadReviewRow(Base):
    __tablename__ = "lead_reviews"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_lead_review_lead"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True, unique=True)
    stage: Mapped[str] = mapped_column(String(32), default="")
    fit: Mapped[str] = mapped_column(String(32))
    pain_level: Mapped[int] = mapped_column(Integer)
    next_action: Mapped[str] = mapped_column(String(32))
    missing_fields: Mapped[str] = mapped_column(String(128), default="")
    follow_up_status: Mapped[str] = mapped_column(String(32), default="")
    follow_up_due_at: Mapped[str] = mapped_column(String(10), default="")
    meeting_status: Mapped[str] = mapped_column(String(32), default="")
    deal_stage: Mapped[str] = mapped_column(String(32), default="")
    conversation_killed: Mapped[bool] = mapped_column(Boolean, default=False)


class GmailThreadSummaryRow(Base):
    __tablename__ = "gmail_thread_summaries"
    __table_args__ = (UniqueConstraint("thread_id", name="uq_gmail_thread_summary"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    message_count: Mapped[int] = mapped_column(Integer)
    intent: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text, default="")


class ApprovalRow(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint(
            "resource_type", "resource_id", "action", name="uq_approval_resource_action"
        ),
        Index(
            "uq_approval_approval_id",
            "approval_id",
            unique=True,
            sqlite_where=text("approval_id != ''"),
            postgresql_where=text("approval_id != ''"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str | None] = mapped_column(
        ForeignKey("leads.id"), index=True, nullable=True
    )
    channel: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32))
    risk: Mapped[str] = mapped_column(String(8))
    payload_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32), default="pending")
    approver: Mapped[str] = mapped_column(String(32), default="")
    resource_type: Mapped[str] = mapped_column(String(32), default="lead")
    # Named approval workflows cap provider-bound identifiers at 80 characters.
    # Calendar and LinkedIn proposal ids include a prefix plus a 40-char digest,
    # so the original VARCHAR(40) could reject otherwise valid approvals.
    resource_id: Mapped[str] = mapped_column(String(80), default="")
    expires_at: Mapped[str] = mapped_column(String(40), default="")
    approval_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    business_id: Mapped[str] = mapped_column(String(32), default="")
    actor_id: Mapped[str] = mapped_column(String(32), default="")
    # Approval payloads are normally compact, but an exact LinkedIn post/comment/upload
    # schema can legitimately exceed 255 characters. The application still applies a
    # conservative action-specific bound before this durable TEXT field is used.
    proposed_parameters: Mapped[str] = mapped_column(Text, default="")
    approved_at: Mapped[str] = mapped_column(String(40), default="")
    executed_at: Mapped[str] = mapped_column(String(40), default="")
    execution_operation_id: Mapped[str] = mapped_column(String(64), default="")
    result: Mapped[str] = mapped_column(String(255), default="")


class OwnerTaskRow(Base):
    __tablename__ = "owner_tasks"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_owner_task_provider_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_event_id: Mapped[str] = mapped_column(String(255), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    task_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[str | None] = mapped_column(String(10), nullable=True)
    trigger: Mapped[str] = mapped_column(String(32), default="none")
    condition: Mapped[str] = mapped_column(String(32), default="none")
    action: Mapped[str] = mapped_column(String(32), default="none")
    due_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[str] = mapped_column(String(32), default="")


class ContentIdeaRow(Base):
    __tablename__ = "content_ideas"
    __table_args__ = (UniqueConstraint("idea_date", name="uq_content_idea_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idea_date: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    kinds: Mapped[str] = mapped_column(Text, default="[]")


class OwnerNotificationRow(Base):
    __tablename__ = "owner_notifications"
    __table_args__ = (
        UniqueConstraint("kind", "lead_id", name="uq_owner_notification_kind_lead"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    lead_id: Mapped[str] = mapped_column(String(32), index=True)
    scheduled_at: Mapped[str] = mapped_column(String(32))
    seen_at: Mapped[str] = mapped_column(String(32), default="")


class OwnerNotificationClaimRow(Base):
    """Send-once ledger for owner notifications, keyed on the CONVERSATION.

    `owner_notifications` is the owner's inbox and is deliberately one row per
    (kind, lead_id) — that is the right key for "this lead has an unseen alert".
    It is the wrong key for "has this brief already been sent": a returning lead's
    second website conversation is a new event the owner must hear about, and keying
    the claim on the lead classified it as a duplicate, so the owner was never told.

    Claims therefore live in their own table. The natural key IS the primary key, so
    the claim is decided by the database, not by a read-then-write in Python, and the
    table needs no sequence — which keeps one DDL text valid on SQLite and PostgreSQL.
    Lead-scoped kinds (hot_lead) claim with an empty conversation_id.
    """

    __tablename__ = "owner_notification_claims"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    lead_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(255), primary_key=True, default="")
    claimed_at: Mapped[str] = mapped_column(String(32), default="")


class OwnerNotificationRecipientClaimRow(Base):
    """Per-recipient notification delivery ledger.

    A Telegram fan-out is not one atomic delivery: accepted owners must not receive a
    retry, known rejected owners must, and ambiguous recipients must remain protected.
    ``delivery_status`` distinguishes a confirmed acceptance from the retained
    ambiguous claim needed for truthful visitor copy on later graph replays.
    """

    __tablename__ = "owner_notification_recipient_claims"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    lead_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    notification_key: Mapped[str] = mapped_column(String(255), primary_key=True, default="")
    recipient_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    claimed_at: Mapped[str] = mapped_column(String(32), default="")
    delivery_status: Mapped[str] = mapped_column(String(16), default="pending")


class OwnerBriefRow(Base):
    __tablename__ = "owner_briefs"
    __table_args__ = (UniqueConstraint("brief_date", name="uq_owner_brief_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brief_date: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    leads: Mapped[int] = mapped_column(Integer, default=0)
    meetings_offered: Mapped[int] = mapped_column(Integer, default=0)
    handoffs: Mapped[int] = mapped_column(Integer, default=0)
    messages_in: Mapped[int] = mapped_column(Integer, default=0)
    follow_ups_due: Mapped[int] = mapped_column(Integer, default=0)
    meetings_booked: Mapped[int] = mapped_column(Integer, default=0)
    cancellation_requests: Mapped[int] = mapped_column(Integer, default=0)


class OwnerWeeklyRow(Base):
    __tablename__ = "owner_weeklies"
    __table_args__ = (UniqueConstraint("week_start", name="uq_owner_weekly_week_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    week_start: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    leads: Mapped[int] = mapped_column(Integer, default=0)
    meetings_offered: Mapped[int] = mapped_column(Integer, default=0)
    handoffs: Mapped[int] = mapped_column(Integer, default=0)
    messages_in: Mapped[int] = mapped_column(Integer, default=0)
    follow_ups_pending: Mapped[int] = mapped_column(Integer, default=0)
    meetings_booked: Mapped[int] = mapped_column(Integer, default=0)
    cancellation_requests: Mapped[int] = mapped_column(Integer, default=0)


class VoiceTranscriptRow(Base):
    __tablename__ = "voice_transcripts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_voice_provider_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_event_id: Mapped[str] = mapped_column(String(255), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    actor_role: Mapped[str] = mapped_column(String(32))
    transcript: Mapped[str] = mapped_column(Text)
    stt_provider: Mapped[str] = mapped_column(String(32), default="")
    stt_model: Mapped[str] = mapped_column(String(64), default="")
    language: Mapped[str] = mapped_column(String(16), default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[str] = mapped_column(String(16), default="")
    cost_usd: Mapped[int] = mapped_column(Integer, default=0)
    retention_status: Mapped[str] = mapped_column(String(16), default="")


class WebhookEventRow(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_webhook_provider_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_event_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), default="processed")
    claimed_at: Mapped[str] = mapped_column(String(64), default="")
    channel: Mapped[str] = mapped_column(String(32), default="")
    envelope_kind: Mapped[str] = mapped_column(String(16), default="")


class ReconciliationFindingRow(Base):
    __tablename__ = "reconciliation_findings"
    __table_args__ = (
        UniqueConstraint("kind", "subject_key", name="uq_reconciliation_kind_subject"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    subject_key: Mapped[str] = mapped_column(String(255), index=True)
    reason: Mapped[str] = mapped_column(String(32))
    open: Mapped[bool] = mapped_column(Boolean, default=True)


class IdentityLinkRow(Base):
    __tablename__ = "identity_links"
    __table_args__ = (UniqueConstraint("identity_id", name="uq_identity_link"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identity_id: Mapped[int] = mapped_column(
        ForeignKey("channel_identities.id"), unique=True
    )
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    reason: Mapped[str] = mapped_column(String(32))
    reversed_at: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)


class HandoffTokenRow(Base):
    __tablename__ = "handoff_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    lead_id: Mapped[str] = mapped_column(String(40), index=True)
    website_session_id: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[str] = mapped_column(String(64))
    consumed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AiRunRow(Base):
    __tablename__ = "ai_runs"
    __table_args__ = (UniqueConstraint("run_id", name="uq_ai_run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    lead_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(32))
    graph_version: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[int] = mapped_column(Integer, default=0)
    next_action: Mapped[str] = mapped_column(String(32))
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_version: Mapped[str] = mapped_column(String(32), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    automation_mode: Mapped[str] = mapped_column(String(32), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    decision_confidence: Mapped[str] = mapped_column(String(16), default="")


class ShadowDecisionRow(Base):
    __tablename__ = "shadow_decisions"
    __table_args__ = (UniqueConstraint("run_id", name="uq_shadow_decision_run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    lead_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(32))
    next_action: Mapped[str] = mapped_column(String(32))
    proposed_reply: Mapped[str] = mapped_column(Text, default="")
    policy_version: Mapped[str] = mapped_column(String(32), default="")


class ToolRunRow(Base):
    __tablename__ = "tool_runs"
    __table_args__ = (
        UniqueConstraint("provider_event_id", name="uq_tool_run_provider_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    lead_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[int] = mapped_column(Integer, default=0)
    freshness: Mapped[str] = mapped_column(String(16), default="")
    correlation_id: Mapped[str] = mapped_column(String(64), default="")


class IdempotencyRow(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(255), index=True)
    created_at: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="completed")
    expires_at: Mapped[str] = mapped_column(String(64), default="")
    result_json: Mapped[str] = mapped_column(Text, default="{}")


class CanonicalEventRow(Base):
    __tablename__ = "canonical_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_canonical_provider_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(255), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_event_id: Mapped[str] = mapped_column(String(255), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(32), index=True)
    occurred_at: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    lead_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_role: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    source_json: Mapped[str] = mapped_column(Text, default="{}")
    correlation_id: Mapped[str] = mapped_column(String(64), default="")
    payload_version: Mapped[str] = mapped_column(String(8), default="")


class ConversationControlRow(Base):
    __tablename__ = "conversation_controls"
    __table_args__ = (
        UniqueConstraint(
            "channel", "external_id", name="uq_conversation_control_channel_external"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    automation_scope: Mapped[str] = mapped_column(String(32), default="unknown")
    source: Mapped[str] = mapped_column(String(64), default="")
    mia_introduced: Mapped[bool] = mapped_column(Boolean, default=False)
    lead_id: Mapped[str] = mapped_column(String(40), default="")


# ---------------------------------------------------------------------------
# Brain: long-term memory, ingested knowledge, entities, gaps.
# Portable column types only (String/Text/Integer/Float) and ISO-8601 timestamp
# strings, so every table below creates identically on SQLite and PostgreSQL and
# needs no POSTGRES_ONLY entry. Embeddings live in a TEXT column as base64
# float32 (app/brain/vectors.py), never a dialect-specific vector type.
# ---------------------------------------------------------------------------


class MemoryRow(Base):
    __tablename__ = "brain_memories"
    __table_args__ = (
        Index("ix_brain_memories_live", "subject", "status", "kind"),
        Index("ix_brain_memories_category", "category", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    memory_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    subject: Mapped[str] = mapped_column(String(64), default="owner", index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    category: Mapped[str] = mapped_column(String(32), default="other")
    text: Mapped[str] = mapped_column(Text)
    importance: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    source: Mapped[str] = mapped_column(String(32), default="telegram")
    source_ref: Mapped[str] = mapped_column(String(255), default="")
    occurred_at: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[str] = mapped_column(String(64), default="")
    last_used_at: Mapped[str] = mapped_column(String(64), default="")
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    superseded_by: Mapped[str] = mapped_column(String(40), default="")
    entities_json: Mapped[str] = mapped_column(Text, default="[]")
    embedding: Mapped[str] = mapped_column(Text, default="")
    embedding_model: Mapped[str] = mapped_column(String(64), default="")
    embedding_dim: Mapped[int] = mapped_column(Integer, default=0)


class KnowledgeSourceRow(Base):
    __tablename__ = "brain_knowledge_sources"
    __table_args__ = (
        UniqueConstraint("source_id", name="uq_brain_knowledge_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(String(500), default="")
    kind: Mapped[str] = mapped_column(String(32), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    fetched_at: Mapped[str] = mapped_column(String(64), default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")
    error: Mapped[str] = mapped_column(String(255), default="")


class KnowledgeChunkRow(Base):
    __tablename__ = "brain_knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("chunk_id", name="uq_brain_chunk_id"),
        Index("ix_brain_chunks_source", "source_id", "status"),
        Index("ix_brain_chunks_category", "category", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(32), default="other")
    title: Mapped[str] = mapped_column(String(255), default="")
    text: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(500), default="")
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    fetched_at: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    embedding: Mapped[str] = mapped_column(Text, default="")
    embedding_model: Mapped[str] = mapped_column(String(64), default="")
    embedding_dim: Mapped[int] = mapped_column(Integer, default=0)


class KnowledgeEntityRow(Base):
    __tablename__ = "brain_entities"
    __table_args__ = (
        UniqueConstraint("entity_key", name="uq_brain_entity_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[str] = mapped_column(String(40), index=True)
    entity_key: Mapped[str] = mapped_column(String(160), index=True)
    kind: Mapped[str] = mapped_column(String(24), default="other")
    name: Mapped[str] = mapped_column(String(160))
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    summary: Mapped[str] = mapped_column(Text, default="")
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[str] = mapped_column(String(64), default="")
    last_seen_at: Mapped[str] = mapped_column(String(64), default="")


class MemoryEntityLinkRow(Base):
    __tablename__ = "brain_memory_entities"
    __table_args__ = (
        UniqueConstraint("memory_id", "entity_key", name="uq_brain_memory_entity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    memory_id: Mapped[str] = mapped_column(String(40), index=True)
    entity_key: Mapped[str] = mapped_column(String(160), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default="")


class KnowledgeGapRow(Base):
    __tablename__ = "brain_knowledge_gaps"
    __table_args__ = (
        UniqueConstraint("gap_id", name="uq_brain_gap_id"),
        Index("ix_brain_gaps_status", "status", "priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gap_id: Mapped[str] = mapped_column(String(40), index=True)
    topic: Mapped[str] = mapped_column(String(160), index=True)
    question: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32), default="other")
    priority: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="open")
    asked_at: Mapped[str] = mapped_column(String(64), default="")
    answered_at: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[str] = mapped_column(String(64), default="")
