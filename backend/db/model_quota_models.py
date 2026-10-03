"""Durable admission counts, never actual provider tokens or billing."""
from sqlalchemy import BigInteger, CheckConstraint, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from backend.db.base import Base

class ModelDailyUsageRecord(Base):
    __tablename__ = "model_daily_usage"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    estimated_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_admitted_at: Mapped[float] = mapped_column(Float, nullable=False)
    __table_args__ = (CheckConstraint("scope IN ('shared', 'history')", name="ck_model_usage_scope"),
                      CheckConstraint("requests >= 0 AND estimated_input_tokens >= 0", name="ck_model_usage_nonnegative"))
