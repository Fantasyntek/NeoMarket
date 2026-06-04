from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import CurrentSeller, require_seller
from app.database import get_db
from app.errors import api_error
from app.models import Category, CharacteristicValue, Product, ProductImage
from app.moderation import (
    ModerationDispatcher,
    build_product_event,
    dispatch_and_mark_sent,
    get_moderation_dispatcher,
    record_outbox_event,
)


router = APIRouter(prefix="/api/v1", tags=["Products"])


def _require_string(payload: dict[str, Any], field: str, max_length: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise api_error(400, "INVALID_REQUEST", f"{field} is required")
    value = value.strip()
    if len(value) > max_length:
        raise api_error(400, "INVALID_REQUEST", f"{field} must be 1-{max_length} characters")
    return value


def _validate_category_id(payload: dict[str, Any], db: Session) -> Category:
    raw_category_id = payload.get("category_id")
    if raw_category_id is None:
        raise api_error(400, "INVALID_REQUEST", "category_id is required")
    if not isinstance(raw_category_id, str):
        raise api_error(400, "INVALID_REQUEST", "category_id must be a valid UUID")
    try:
        category_id = str(UUID(raw_category_id))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", "category_id must be a valid UUID")

    category = db.get(Category, category_id)
    if category is None:
        raise api_error(400, "INVALID_REQUEST", "Category not found")
    return category


def _validate_images(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_images = payload.get("images")
    if not isinstance(raw_images, list) or not raw_images:
        raise api_error(400, "INVALID_REQUEST", "At least one image is required")

    images: list[dict[str, Any]] = []
    for index, raw_image in enumerate(raw_images):
        if not isinstance(raw_image, dict):
            raise api_error(400, "INVALID_REQUEST", f"images[{index}] must be an object")
        url = raw_image.get("url")
        if not isinstance(url, str) or not url.strip():
            raise api_error(400, "INVALID_REQUEST", f"images[{index}].url is required")
        ordering = raw_image.get("ordering")
        if not isinstance(ordering, int) or ordering < 0:
            raise api_error(
                400,
                "INVALID_REQUEST",
                f"images[{index}].ordering must be a non-negative integer",
            )
        images.append({"url": url.strip(), "ordering": ordering})
    return images


def _validate_characteristics(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_characteristics = payload.get("characteristics", [])
    if raw_characteristics is None:
        return []
    if not isinstance(raw_characteristics, list):
        raise api_error(400, "INVALID_REQUEST", "characteristics must be an array")

    characteristics: list[dict[str, str]] = []
    for index, raw_characteristic in enumerate(raw_characteristics):
        if not isinstance(raw_characteristic, dict):
            raise api_error(400, "INVALID_REQUEST", f"characteristics[{index}] must be an object")
        name = raw_characteristic.get("name")
        value = raw_characteristic.get("value")
        if not isinstance(name, str) or not name.strip():
            raise api_error(400, "INVALID_REQUEST", f"characteristics[{index}].name is required")
        if not isinstance(value, str) or not value.strip():
            raise api_error(400, "INVALID_REQUEST", f"characteristics[{index}].value is required")
        characteristics.append({"name": name.strip(), "value": value.strip()})
    return characteristics


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "product"


def _serialize_product(product: Product) -> dict[str, Any]:
    return {
        "id": product.id,
        "seller_id": product.seller_id,
        "category_id": product.category_id,
        "title": product.title,
        "slug": product.slug,
        "description": product.description,
        "status": product.status,
        "deleted": product.deleted,
        "blocked": product.blocked,
        "blocking_reason_id": product.blocking_reason_id,
        "moderator_comment": product.moderator_comment,
        "category": {"id": product.category.id, "name": product.category.name},
        "images": [
            {"id": image.id, "url": image.url, "ordering": image.ordering}
            for image in product.images
        ],
        "characteristics": [
            {"id": item.id, "name": item.name, "value": item.value}
            for item in product.characteristics
        ],
        "skus": [
            {
                "id": sku.id,
                "product_id": sku.product_id,
                "name": sku.name,
                "price": sku.price,
                "cost_price": sku.cost_price,
                "discount": sku.discount,
                "image": sku.image,
                "active_quantity": sku.active_quantity,
                "reserved_quantity": sku.reserved_quantity,
            }
            for sku in product.skus
        ],
        "created_at": product.created_at.isoformat(),
        "updated_at": product.updated_at.isoformat(),
    }


@router.post("/products", status_code=201)
def create_product(
    payload: dict[str, Any],
    current_seller: CurrentSeller = Depends(require_seller),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    title = _require_string(payload, "title", 255)
    description = _require_string(payload, "description", 5000)
    category = _validate_category_id(payload, db)
    images = _validate_images(payload)
    characteristics = _validate_characteristics(payload)

    product = Product(
        seller_id=current_seller.seller_id,
        category_id=category.id,
        title=title,
        slug=str(payload.get("slug") or _slugify(title)),
        description=description,
        status="CREATED",
    )
    product.images = [
        ProductImage(url=image["url"], ordering=image["ordering"]) for image in images
    ]
    product.characteristics = [
        CharacteristicValue(name=item["name"], value=item["value"])
        for item in characteristics
    ]

    db.add(product)
    db.commit()
    db.refresh(product)
    return _serialize_product(product)


@router.put("/products/{product_id}")
def update_product(
    product_id: str,
    payload: dict[str, Any],
    current_seller: CurrentSeller = Depends(require_seller),
    db: Session = Depends(get_db),
    moderation_dispatcher: ModerationDispatcher = Depends(get_moderation_dispatcher),
) -> dict[str, Any]:
    try:
        normalized_product_id = str(UUID(product_id))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", "product_id must be a valid UUID")

    product = db.get(Product, normalized_product_id)
    if product is None:
        raise api_error(404, "NOT_FOUND", "Product not found")
    if product.seller_id != current_seller.seller_id:
        raise api_error(
            403,
            "NOT_OWNER",
            "Product does not belong to the authenticated seller",
        )
    if product.status == "HARD_BLOCKED":
        raise api_error(403, "FORBIDDEN", "Cannot edit hard-blocked product")

    title = _require_string(payload, "title", 255)
    description = _require_string(payload, "description", 5000)
    category = _validate_category_id(payload, db)
    images = _validate_images(payload)
    characteristics = _validate_characteristics(payload)

    product.title = title
    product.slug = str(payload.get("slug") or _slugify(title))
    product.description = description
    product.category_id = category.id
    product.images = [
        ProductImage(url=image["url"], ordering=image["ordering"]) for image in images
    ]
    product.characteristics = [
        CharacteristicValue(name=item["name"], value=item["value"])
        for item in characteristics
    ]

    event_payload: dict[str, Any] | None = None
    outbox_event = None
    if product.status in {"MODERATED", "BLOCKED"}:
        product.status = "ON_MODERATION"
        event_payload = build_product_event(product, "EDITED")
        outbox_event = record_outbox_event(db, event_payload)

    db.commit()
    db.refresh(product)
    dispatch_and_mark_sent(db, moderation_dispatcher, event_payload, outbox_event)
    return _serialize_product(product)
