from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def new_uuid() -> str:
    return str(uuid4())


class ProductModeration(Base):
    __tablename__ = "product_moderation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'IN_REVIEW', 'MODERATED', 'BLOCKED', "
            "'HARD_BLOCKED', 'ARCHIVED')",
            name="ck_product_moderation_status",
        ),
        CheckConstraint(
            "queue_priority >= 1 AND queue_priority <= 4",
            name="ck_product_moderation_queue_priority",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        unique=True,
        index=True,
    )
    seller_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    category_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    queue_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    json_before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    json_after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    total_active_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    moderator_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    blocking_reason_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    moderator_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    date_created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    date_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ProcessedProductEvent(Base):
    __tablename__ = "processed_product_events"

    idempotency_key: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
