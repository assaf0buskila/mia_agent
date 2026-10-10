"""Canonical durable owner reminders."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OwnerReminderRow(Base):
    __tablename__ = "owner_reminders"
    __table_args__ = (
        UniqueConstraint("owner_id", "source_event_id", name="uq_owner_reminder_source"),
        Index("ix_owner_reminders_due", "owner_id", "status", "scheduled_at"),
        CheckConstraint(
            "status IN ('active', 'completed', 'cancelled')",
            name="ck_owner_reminders_status",
        ),
        CheckConstraint(
            "delivery_state IN "
            "('pending', 'dispatching', 'accepted', 'unknown', 'rejected')",
            name="ck_owner_reminders_delivery_state",
        ),
        CheckConstraint("revision >= 1", name="ck_owner_reminders_revision"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Jerusalem")
    contact_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("crm_contacts.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    delivery_state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
