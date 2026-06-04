from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from app.auth import CurrentSeller, decode_access_token, is_valid_service_key, require_seller
from app.b2c import (
    B2CDispatcher,
    build_product_deleted_event,
    dispatch_b2c_and_mark_sent,
    get_b2c_dispatcher,
    record_b2c_outbox_event,
)
from app.database import get_db
from app.errors import api_error
from app.models import Category, CharacteristicValue, Product, ProductImage, SKU
from app.moderation import (
    ModerationDispatcher,
    build_product_event,
    dispatch_and_mark_sent,
    get_moderation_dispatcher,
    record_outbox_event,
)


router = APIRouter(prefix="/api/v1", tags=["Products"])


PRODUCT_STATUSES = {"CREATED", "ON_MODERATION", "MODERATED", "BLOCKED", "HARD_BLOCKED"}


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


def _normalize_product_id(product_id: str) -> str:
    return _normalize_uuid(product_id, "product_id")


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", f"{field_name} must be a valid UUID")


def _seller_from_authorization(authorization: str | None) -> CurrentSeller:
    if not authorization or not authorization.startswith("Bearer "):
        raise api_error(401, "UNAUTHORIZED", "Authorization required")

    payload = decode_access_token(authorization.removeprefix("Bearer ").strip())
    seller_id = payload.get("seller_id")
    if not isinstance(seller_id, str) or not seller_id:
        raise api_error(401, "UNAUTHORIZED", "seller_id claim is required")
    return CurrentSeller(seller_id=seller_id)


def _serialize_blocking_reason(product: Product) -> dict[str, str] | None:
    if product.blocking_reason_id is None:
        return None
    return {
        "id": product.blocking_reason_id,
        "title": product.blocking_reason_title or "",
        "comment": product.moderator_comment or "",
    }


def _serialize_sku_for_product(sku: Any, include_sensitive: bool) -> dict[str, Any]:
    payload = {
        "id": sku.id,
        "product_id": sku.product_id,
        "name": sku.name,
        "price": sku.price,
        "discount": sku.discount,
        "image": sku.image,
        "active_quantity": sku.active_quantity,
        "characteristics": [
            {"id": item.id, "name": item.name, "value": item.value}
            for item in sku.characteristics
        ],
    }
    if include_sensitive:
        payload["cost_price"] = sku.cost_price
        payload["reserved_quantity"] = sku.reserved_quantity
    return payload


def _serialize_catalog_product(product: Product) -> dict[str, Any]:
    visible_skus = [sku for sku in product.skus if sku.active_quantity > 0]
    return {
        "id": product.id,
        "title": product.title,
        "description": product.description,
        "status": product.status,
        "category": {"id": product.category.id, "name": product.category.name},
        "images": [
            {"url": image.url, "ordering": image.ordering}
            for image in product.images
        ],
        "characteristics": [
            {"name": item.name, "value": item.value}
            for item in product.characteristics
        ],
        "skus": [
            _serialize_sku_for_product(sku, include_sensitive=False)
            for sku in visible_skus
        ],
    }


def _serialize_product(product: Product, include_sensitive: bool = True) -> dict[str, Any]:
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
        "blocking_reason": _serialize_blocking_reason(product),
        "category": {"id": product.category.id, "name": product.category.name},
        "images": [
            {"id": image.id, "url": image.url, "ordering": image.ordering}
            for image in product.images
        ],
        "characteristics": [
            {"id": item.id, "name": item.name, "value": item.value}
            for item in product.characteristics
        ],
        "skus": [_serialize_sku_for_product(sku, include_sensitive) for sku in product.skus],
        "field_reports": [
            {
                "field_name": report.field_name,
                "sku_id": report.sku_id,
                "comment": report.comment,
            }
            for report in product.field_reports
        ],
        "created_at": product.created_at.isoformat(),
        "updated_at": product.updated_at.isoformat(),
    }


def _serialize_product_list_item(product: Product) -> dict[str, Any]:
    return {
        "id": product.id,
        "title": product.title,
        "status": product.status,
        "category": {"id": product.category.id, "name": product.category.name},
        "images": [
            {"url": image.url, "ordering": image.ordering}
            for image in product.images
        ],
        "skus_count": len(product.skus),
        "total_active_quantity": sum(sku.active_quantity for sku in product.skus),
        "created_at": product.created_at.isoformat(),
    }


def _parse_ids_filter(ids: str | None) -> list[str] | None:
    if ids is None or not ids.strip():
        return None
    product_ids = [item.strip() for item in ids.split(",") if item.strip()]
    if not product_ids:
        return None
    return [_normalize_uuid(product_id, "ids") for product_id in product_ids]


def _list_catalog_products(
    db: Session,
    limit: int,
    offset: int,
    category: str | None,
    search: str | None,
    sort: str | None,
    ids: str | None,
) -> dict[str, Any]:
    product_ids = _parse_ids_filter(ids)
    normalized_category = _normalize_uuid(category, "category") if category else None

    query = db.query(Product).filter(
        Product.status == "MODERATED",
        Product.deleted.is_(False),
        Product.skus.any(SKU.active_quantity > 0),
    )
    if normalized_category is not None:
        query = query.filter(Product.category_id == normalized_category)
    if search is not None and search.strip():
        search_pattern = f"%{search.strip()}%"
        query = query.filter(
            Product.title.ilike(search_pattern) | Product.description.ilike(search_pattern)
        )
    if product_ids is not None:
        query = query.filter(Product.id.in_(product_ids))

    total_count = query.count()
    if sort == "date_desc" or sort is None:
        query = query.order_by(Product.created_at.desc())
    elif sort == "price_asc":
        query = query.order_by(Product.created_at.desc())
    elif sort == "price_desc":
        query = query.order_by(Product.created_at.desc())
    else:
        raise api_error(400, "INVALID_REQUEST", "sort is invalid")

    products = query.offset(offset).limit(limit).all()
    return {
        "items": [_serialize_catalog_product(product) for product in products],
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }


@router.get("/products")
def list_products(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = None,
    search: str | None = None,
    category: str | None = None,
    sort: str | None = None,
    ids: str | None = None,
) -> dict[str, Any]:
    if x_service_key is not None:
        if not is_valid_service_key(x_service_key):
            raise api_error(401, "UNAUTHORIZED", "Invalid service key")
        return _list_catalog_products(db, limit, offset, category, search, sort, ids)

    current_seller = _seller_from_authorization(authorization)
    if status is not None and status not in PRODUCT_STATUSES:
        raise api_error(400, "INVALID_REQUEST", "status is invalid")

    query = db.query(Product).filter(
        Product.seller_id == current_seller.seller_id,
        Product.deleted.is_(False),
    )
    if status is not None:
        query = query.filter(Product.status == status)
    if search is not None and search.strip():
        query = query.filter(Product.title.ilike(f"%{search.strip()}%"))

    total_count = query.count()
    products = query.order_by(Product.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "items": [_serialize_product_list_item(product) for product in products],
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
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


@router.get("/products/{product_id}")
def get_product(
    product_id: str,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
) -> dict[str, Any]:
    normalized_product_id = _normalize_uuid(product_id, "id")
    product = db.get(Product, normalized_product_id)
    if product is None:
        raise api_error(404, "NOT_FOUND", "Product not found")

    if is_valid_service_key(x_service_key):
        return _serialize_product(product, include_sensitive=False)

    current_seller = _seller_from_authorization(authorization)
    if product.seller_id != current_seller.seller_id:
        raise api_error(404, "NOT_FOUND", "Product not found")
    return _serialize_product(product, include_sensitive=True)


@router.put("/products/{product_id}")
def update_product(
    product_id: str,
    payload: dict[str, Any],
    current_seller: CurrentSeller = Depends(require_seller),
    db: Session = Depends(get_db),
    moderation_dispatcher: ModerationDispatcher = Depends(get_moderation_dispatcher),
) -> dict[str, Any]:
    normalized_product_id = _normalize_product_id(product_id)

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


@router.delete("/products/{product_id}")
def delete_product(
    product_id: str,
    current_seller: CurrentSeller = Depends(require_seller),
    db: Session = Depends(get_db),
    moderation_dispatcher: ModerationDispatcher = Depends(get_moderation_dispatcher),
    b2c_dispatcher: B2CDispatcher = Depends(get_b2c_dispatcher),
) -> dict[str, bool]:
    normalized_product_id = _normalize_product_id(product_id)
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
        raise api_error(403, "FORBIDDEN", "Cannot delete hard-blocked product")
    if product.deleted:
        raise api_error(400, "INVALID_REQUEST", "Product already deleted")

    sku_ids = [sku.id for sku in product.skus]
    product.deleted = True

    moderation_payload = build_product_event(product, "DELETED")
    moderation_outbox_event = record_outbox_event(db, moderation_payload)
    b2c_payload = build_product_deleted_event(product, sku_ids)
    b2c_outbox_event = record_b2c_outbox_event(db, b2c_payload)

    db.commit()
    dispatch_and_mark_sent(
        db,
        moderation_dispatcher,
        moderation_payload,
        moderation_outbox_event,
    )
    dispatch_b2c_and_mark_sent(db, b2c_dispatcher, b2c_payload, b2c_outbox_event)
    return {"ok": True}
