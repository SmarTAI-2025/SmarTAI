"""Global anonymous-demo admission survives restarts and spans worker processes."""
from sqlalchemy import BigInteger, CheckConstraint, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base


class FrontierDemoDailyUsageRecord(Base):
    __tablename__ = "frontier_demo_daily_usage"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    sessions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    estimated_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_session_at: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    __table_args__ = (CheckConstraint(
        "sessions >= 0 AND requests >= 0 AND estimated_input_tokens >= 0",
        name="ck_frontier_demo_usage_nonnegative"),)
