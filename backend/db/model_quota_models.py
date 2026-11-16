"""Legacy daily admission counters and actual-use weekly shared charges."""
from sqlalchemy import BigInteger, CheckConstraint, Float, ForeignKey, Integer, String, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column
from backend.db.base import Base

class ModelDailyUsageRecord(Base):
    __tablename__ = "model_daily_usage"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    estimated_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sol_requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    sol_estimated_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    last_admitted_at: Mapped[float] = mapped_column(Float, nullable=False)
    __table_args__ = (CheckConstraint("scope IN ('shared', 'history')", name="ck_model_usage_scope"),
                      CheckConstraint("requests >= 0 AND estimated_input_tokens >= 0", name="ck_model_usage_nonnegative"))


class SharedModelChargeRecord(Base):
    """One upstream attempt; unknown charges retain their reservation, never zero."""
    __tablename__ = "shared_model_charges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    week: Mapped[str] = mapped_column(String(10), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reserved_nano_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_nano_usd: Mapped[int | None] = mapped_column(BigInteger)
    usage: Mapped[dict | None] = mapped_column(JSON)
    response_id: Mapped[str | None] = mapped_column(String(128))
    price_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    settled_at: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        Index("ix_shared_charges_owner_week", "owner_id", "week"),
        CheckConstraint("state IN ('reserved', 'settled', 'unknown', 'released')", name="ck_shared_charge_state"),
        CheckConstraint("reserved_nano_usd >= 0 AND (cost_nano_usd IS NULL OR cost_nano_usd >= 0)", name="ck_shared_charge_nonnegative"),
    )
