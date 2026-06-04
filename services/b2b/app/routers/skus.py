from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import CurrentSeller, require_seller
from app.database import get_db
from app.errors import api_error
from app.models import (
    ModerationOutboxEvent,
    Product,
    SKU,
    SKUCharacteristicValue,
)
from app.moderation import (
    ModerationDispatcher,
    build_product_event,
    dispatch_and_mark_sent,
    get_moderation_dispatcher,
    record_outbox_event,
)


router = APIRouter(prefix="/api/v1", tags=["SKUs"])


def _require_uuid(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise api_error(400, "INVALID_REQUEST", f"{field} is required")
    try:
        return str(UUID(value))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", f"{field} must be a valid UUID")


def _require_string(payload: dict[str, Any], field: str, max_length: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise api_error(400, "INVALID_REQUEST", f"{field} is required")
    value = value.strip()
    if len(value) > max_length:
        raise api_error(400, "INVALID_REQUEST", f"{field} must be 1-{max_length} characters")
    return value


def _require_positive_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or value <= 0:
        raise api_error(
            400,
            "INVALID_REQUEST",
            f"{field} must be a positive integer (kopecks)",
        )
    return value


def _optional_non_negative_int(payload: dict[str, Any], field: str, default: int) -> int:
    value = payload.get(field, default)
    if not isinstance(value, int) or value < 0:
        raise api_error(400, "INVALID_REQUEST", f"{field} must be a non-negative integer")
    return value


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


def _serialize_sku(sku: SKU) -> dict[str, Any]:
    return {
        "id": sku.id,
        "product_id": sku.product_id,
        "name": sku.name,
        "price": sku.price,
        "cost_price": sku.cost_price,
        "discount": sku.discount,
        "image": sku.image,
        "stock_quantity": sku.active_quantity,
        "active_quantity": sku.active_quantity,
        "reserved_quantity": sku.reserved_quantity,
        "images": [{"url": sku.image, "ordering": 0}],
        "characteristics": [
            {"id": item.id, "name": item.name, "value": item.value}
            for item in sku.characteristics
        ],
        "created_at": sku.created_at.isoformat(),
        "updated_at": sku.updated_at.isoformat(),
    }


@router.post("/skus", status_code=201)
def create_sku(
    payload: dict[str, Any],
    current_seller: CurrentSeller = Depends(require_seller),
    db: Session = Depends(get_db),
    moderation_dispatcher: ModerationDispatcher = Depends(get_moderation_dispatcher),
) -> dict[str, Any]:
    product_id = _require_uuid(payload, "product_id")
    name = _require_string(payload, "name", 255)
    price = _require_positive_int(payload, "price")
    cost_price = _require_positive_int(payload, "cost_price")
    discount = _optional_non_negative_int(payload, "discount", 0)
    image = _require_string(payload, "image", 2048)
    characteristics = _validate_characteristics(payload)

    product = db.get(Product, product_id)
    if product is None:
        raise api_error(404, "NOT_FOUND", "Product not found")
    if product.seller_id != current_seller.seller_id:
        raise api_error(
            403,
            "NOT_OWNER",
            "Product does not belong to the authenticated seller",
        )
    if product.status == "HARD_BLOCKED":
        raise api_error(403, "FORBIDDEN", "Cannot add SKU to hard-blocked product")

    was_without_skus = len(product.skus) == 0
    event_payload: dict[str, Any] | None = None
    outbox_event: ModerationOutboxEvent | None = None

    sku = SKU(
        product_id=product.id,
        name=name,
        price=price,
        cost_price=cost_price,
        discount=discount,
        image=image,
        active_quantity=0,
        reserved_quantity=0,
    )
    sku.characteristics = [
        SKUCharacteristicValue(name=item["name"], value=item["value"])
        for item in characteristics
    ]
    db.add(sku)

    if was_without_skus and product.status == "CREATED":
        product.status = "ON_MODERATION"
        event_payload = build_product_event(product, "CREATED")
        outbox_event = record_outbox_event(db, event_payload)

    db.commit()
    db.refresh(sku)

    dispatch_and_mark_sent(db, moderation_dispatcher, event_payload, outbox_event)

    return _serialize_sku(sku)


@router.put("/skus/{sku_id}")
def update_sku(
    sku_id: str,
    payload: dict[str, Any],
    current_seller: CurrentSeller = Depends(require_seller),
    db: Session = Depends(get_db),
    moderation_dispatcher: ModerationDispatcher = Depends(get_moderation_dispatcher),
) -> dict[str, Any]:
    try:
        normalized_sku_id = str(UUID(sku_id))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", "sku_id must be a valid UUID")

    sku = db.get(SKU, normalized_sku_id)
    if sku is None:
        raise api_error(404, "NOT_FOUND", "SKU not found")

    product = sku.product
    if product.seller_id != current_seller.seller_id:
        raise api_error(
            403,
            "NOT_OWNER",
            "SKU does not belong to the authenticated seller",
        )
    if product.status == "HARD_BLOCKED":
        raise api_error(403, "FORBIDDEN", "Cannot edit hard-blocked product")

    name = _require_string(payload, "name", 255)
    price = _require_positive_int(payload, "price")
    cost_price = _require_positive_int(payload, "cost_price")
    discount = _optional_non_negative_int(payload, "discount", 0)
    image = _require_string(payload, "image", 2048)
    characteristics = _validate_characteristics(payload)

    sku.name = name
    sku.price = price
    sku.cost_price = cost_price
    sku.discount = discount
    sku.image = image
    sku.characteristics = [
        SKUCharacteristicValue(name=item["name"], value=item["value"])
        for item in characteristics
    ]

    event_payload: dict[str, Any] | None = None
    outbox_event: ModerationOutboxEvent | None = None
    if product.status in {"MODERATED", "BLOCKED"}:
        product.status = "ON_MODERATION"
        event_payload = build_product_event(product, "EDITED")
        outbox_event = record_outbox_event(db, event_payload)

    db.commit()
    db.refresh(sku)
    dispatch_and_mark_sent(db, moderation_dispatcher, event_payload, outbox_event)
    return _serialize_sku(sku)
