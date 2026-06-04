from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def new_uuid() -> str:
    return str(uuid4())


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    products: Mapped[list[Product]] = relationship(back_populates="category")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    seller_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    category_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("categories.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="CREATED")
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocking_reason_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    blocking_reason_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    moderator_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    category: Mapped[Category] = relationship(back_populates="products")
    images: Mapped[list[ProductImage]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductImage.ordering",
    )
    characteristics: Mapped[list[CharacteristicValue]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    skus: Mapped[list[SKU]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    field_reports: Mapped[list[ProductFieldReport]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductImage(Base):
    __tablename__ = "product_images"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    ordering: Mapped[int] = mapped_column(Integer, nullable=False)

    product: Mapped[Product] = relationship(back_populates="images")


class CharacteristicValue(Base):
    __tablename__ = "characteristic_values"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    product: Mapped[Product] = relationship(back_populates="characteristics")


class SKU(Base):
    __tablename__ = "skus"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_price: Mapped[int] = mapped_column(Integer, nullable=False)
    discount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    image: Mapped[str] = mapped_column(Text, nullable=False)
    active_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    product: Mapped[Product] = relationship(back_populates="skus")
    characteristics: Mapped[list[SKUCharacteristicValue]] = relationship(
        back_populates="sku", cascade="all, delete-orphan"
    )
    invoice_items: Mapped[list[InvoiceItem]] = relationship(back_populates="sku")


class SKUCharacteristicValue(Base):
    __tablename__ = "sku_characteristic_values"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sku_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skus.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    sku: Mapped[SKU] = relationship(back_populates="characteristics")


class ProductFieldReport(Base):
    __tablename__ = "product_field_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id"), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    sku_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    comment: Mapped[str] = mapped_column(Text, nullable=False)

    product: Mapped[Product] = relationship(back_populates="field_reports")


class ModerationOutboxEvent(Base):
    __tablename__ = "moderation_outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    seller_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class B2COutboxEvent(Base):
    __tablename__ = "b2c_outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReserveOperation(Base):
    __tablename__ = "reserve_operations"

    idempotency_key: Mapped[str] = mapped_column(String(36), primary_key=True)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UnreserveOperation(Base):
    __tablename__ = "unreserve_operations"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProcessedModerationEvent(Base):
    __tablename__ = "processed_moderation_events"

    idempotency_key: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    seller_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    items: Mapped[list[InvoiceItem]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
    )


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("invoices.id"), nullable=False, index=True
    )
    sku_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skus.id"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)

    invoice: Mapped[Invoice] = relationship(back_populates="items")
    sku: Mapped[SKU] = relationship(back_populates="invoice_items")
