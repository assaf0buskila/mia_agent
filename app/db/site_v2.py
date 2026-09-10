"""Persistence rows for the isolated public website v2 conversation loop."""

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SiteV2SessionRow(Base):
    __tablename__ = "site_v2_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    credential_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    active_message_id: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[str] = mapped_column(String(64), default="")
    ended_at: Mapped[str] = mapped_column(String(64), default="")


class SiteV2MessageRow(Base):
    __tablename__ = "site_v2_messages"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "client_message_id", name="uq_site_v2_session_message"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("site_v2_sessions.session_id"), index=True
    )
    client_message_id: Mapped[str] = mapped_column(String(120))
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="processing")
    response_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[str] = mapped_column(String(64), default="")
