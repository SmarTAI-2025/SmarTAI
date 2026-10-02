"""Business overrides only; deployment secrets never enter these tables."""
from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer, JSON, String, text
from sqlalchemy.orm import Mapped, mapped_column
from backend.db.base import Base


class BusinessConfigRecord(Base):
    __tablename__ = "business_configuration"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    overrides: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Sticky until a complete business reset: users admitted under past rules
    # must retain password recovery even after overrides return to inheritance.
    registration_rules_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)
    __table_args__ = (CheckConstraint("id = 'global'", name="ck_business_configuration_singleton"),
                      CheckConstraint("version >= 1", name="ck_business_configuration_version"))


class UserStorageConfigRecord(Base):
    __tablename__ = "user_storage_configuration"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    overrides: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)
    __table_args__ = (CheckConstraint("version >= 1", name="ck_user_storage_configuration_version"),)
