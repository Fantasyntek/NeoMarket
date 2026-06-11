from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def new_uuid() -> str:
    return str(uuid4())


class ProductModeration(Base):
    __tablename__ = "product_moderation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'IN_REVIEW', 'APPROVED', 'BLOCKED', "
            "'HARD_BLOCKED', 'ARCHIVED')",
            name="ck_product_moderation_status",
        ),
        CheckConstraint(
            "queue_priority >= 1 AND queue_priority <= 4",
            name="ck_product_moderation_queue_priority",
        ),
        Index(
            "uq_product_moderation_active_moderator",
            "moderator_id",
            unique=True,
            sqlite_where=text("status = 'IN_REVIEW'"),
            postgresql_where=text("status = 'IN_REVIEW'"),
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
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="CREATE")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    queue_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    json_before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    json_after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    total_active_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    review_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    moderator_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    blocking_reason_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    moderator_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
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


class B2BOutboxEvent(Base):
    __tablename__ = "b2b_outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    idempotency_key: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        unique=True,
        index=True,
    )
    product_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class BlockingReason(Base):
    __tablename__ = "blocking_reasons"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hard_block: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ModerationFieldReport(Base):
    __tablename__ = "moderation_field_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_moderation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("product_moderation.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    field_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    sku_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="ERROR")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
