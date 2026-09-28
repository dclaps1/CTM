"""Database models. All datetimes are stored as naive UTC."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

ROLES = ("admin", "manager", "agent")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Agent(Base):
    """A CallTrackingMetrics user who handles calls."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class User(Base):
    """A person who signs in to this app (admin, manager or agent)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="agent")
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    agent: Mapped[Agent | None] = relationship()

    @property
    def is_manager(self) -> bool:
        return self.role in ("admin", "manager")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Call(Base):
    """A call pulled from CTM (inbound or outbound voice calls only)."""

    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    called_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    direction: Mapped[str] = mapped_column(String(16), default="inbound")
    status: Mapped[str] = mapped_column(String(64), default="")
    answered: Mapped[bool] = mapped_column(Boolean, default=False)
    voicemail: Mapped[bool] = mapped_column(Boolean, default=False)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    talk_time: Mapped[int] = mapped_column(Integer, default=0)
    ring_time: Mapped[int] = mapped_column(Integer, default=0)
    caller_number: Mapped[str] = mapped_column(String(64), default="", index=True)
    caller_name: Mapped[str] = mapped_column(String(255), default="")
    caller_city: Mapped[str] = mapped_column(String(128), default="")
    caller_state: Mapped[str] = mapped_column(String(64), default="")
    tracking_number: Mapped[str] = mapped_column(String(64), default="")
    tracking_label: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(255), default="", index=True)
    receiving_number: Mapped[str] = mapped_column(String(64), default="")
    agent_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_name: Mapped[str] = mapped_column(String(255), default="")
    tags: Mapped[str] = mapped_column(Text, default="")
    is_new_caller: Mapped[bool] = mapped_column(Boolean, default=False)
    sale_score: Mapped[int | None] = mapped_column(Integer)
    sale_conversion: Mapped[bool | None] = mapped_column(Boolean)
    sale_value: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    audio_url: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    reviews: Mapped[list[CallReview]] = relationship(
        back_populates="call",
        order_by="CallReview.created_at.desc()",
        cascade="all, delete-orphan",
    )

    @property
    def is_inbound(self) -> bool:
        return self.direction == "inbound"

    @property
    def missed(self) -> bool:
        return self.is_inbound and not self.answered

    @property
    def outcome(self) -> str:
        if self.answered:
            return "answered"
        if self.voicemail:
            return "voicemail"
        return "missed" if self.is_inbound else "no answer"

    @property
    def tag_list(self) -> list[str]:
        return [t for t in (self.tags or "").split(",") if t]

    @property
    def has_recording(self) -> bool:
        return bool(self.audio_url)


class CallReview(Base):
    """A manager's QA scorecard and coaching feedback on one call."""

    __tablename__ = "call_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id"), index=True)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    agent_id: Mapped[str | None] = mapped_column(String(64), index=True)
    score: Mapped[float] = mapped_column(Float)  # percent, 0-100
    criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    feedback: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    pushed_to_ctm: Mapped[bool] = mapped_column(Boolean, default=False)

    call: Mapped[Call] = relationship(back_populates="reviews")
    reviewer: Mapped[User] = relationship()


class SyncState(Base):
    """Key/value bookkeeping for the CTM sync (cursor, last result, last error)."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
