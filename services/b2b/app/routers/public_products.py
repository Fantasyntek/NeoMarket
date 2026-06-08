from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.orm import Session

from app.auth import is_valid_service_key
from app.database import get_db
from app.errors import api_error
from app.models import Product, SKU


router = APIRouter(prefix="/api/v1/public", tags=["Public Catalog"])

PUBLIC_SORTS = {"price_asc", "price_desc", "created_desc", "popular"}


def _require_service_key(x_service_key: str | None) -> None:
    if not is_valid_service_key(x_service_key):
        raise api_error(401, "UNAUTHORIZED", "Invalid service key")


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", f"{field_name} must be a valid UUID")


def _is_public_product(product: Product) -> bool:
    return (
        product.status == "MODERATED"
        and not product.deleted
        and any(sku.active_quantity > 0 for sku in product.skus)
    )


def _effective_price(sku: SKU) -> int:
    return max(sku.price - sku.discount, 0)


def _minimum_price(product: Product) -> int:
    prices = [
        _effective_price(sku)
        for sku in product.skus
        if sku.active_quantity > 0
    ]
    return min(prices) if prices else 0


def _serialize_public_sku(sku: SKU) -> dict[str, Any]:
    return {
        "id": sku.id,
        "product_id": sku.product_id,
        "name": sku.name,
        "price": sku.price,
        "discount": sku.discount,
        "stock_quantity": sku.active_quantity,
        "active_quantity": sku.active_quantity,
        "article": None,
        "image": sku.image,
        "images": [{"id": sku.id, "url": sku.image, "ordering": 0}],
        "characteristics": [
            {"id": item.id, "name": item.name, "value": item.value}
            for item in sku.characteristics
        ],
    }


def _serialize_public_product(
    product: Product,
    include_out_of_stock_skus: bool,
) -> dict[str, Any]:
    skus = (
        product.skus
        if include_out_of_stock_skus
        else [sku for sku in product.skus if sku.active_quantity > 0]
    )
    cover_image = product.images[0].url if product.images else None
    return {
        "id": product.id,
        "seller_id": product.seller_id,
        "category_id": product.category_id,
        "title": product.title,
        "slug": product.slug,
        "description": product.description,
        "status": product.status,
        "category": {"id": product.category.id, "name": product.category.name},
        "images": [
            {"id": image.id, "url": image.url, "ordering": image.ordering}
            for image in product.images
        ],
        "characteristics": [
            {"id": item.id, "name": item.name, "value": item.value}
            for item in product.characteristics
        ],
        "skus": [_serialize_public_sku(sku) for sku in skus],
        "min_price": _minimum_price(product),
        "cover_image": cover_image,
        "created_at": product.created_at.isoformat(),
        "updated_at": product.updated_at.isoformat(),
    }


def _is_public_product_content(product: Product) -> bool:
    return product.status == "MODERATED" and not product.deleted


def _serialize_public_sku_lookup(sku: SKU) -> dict[str, Any]:
    return {
        "product": {
            "id": sku.product.id,
            "title": sku.product.title,
            "status": sku.product.status,
            "deleted": sku.product.deleted,
        },
        "sku": _serialize_public_sku(sku),
    }


def _parse_deep_filters(request: Request) -> dict[str, set[str]]:
    filters: dict[str, set[str]] = defaultdict(set)
    for key, value in request.query_params.multi_items():
        if key.startswith("filters[") and key.endswith("]"):
            filters[key[8:-1].strip().lower()].add(value.strip().lower())
    return filters


def _normalize_characteristic_name(name: str) -> str:
    return name.strip().lower().replace("_", " ").replace("-", " ")


def _matches_filters(product: Product, filters: dict[str, set[str]]) -> bool:
    if not filters:
        return True

    available: dict[str, set[str]] = defaultdict(set)
    for item in product.characteristics:
        available[_normalize_characteristic_name(item.name)].add(item.value.strip().lower())
    for sku in product.skus:
        if sku.active_quantity <= 0:
            continue
        for item in sku.characteristics:
            available[_normalize_characteristic_name(item.name)].add(item.value.strip().lower())

    return all(
        bool(available.get(_normalize_characteristic_name(name), set()) & values)
        for name, values in filters.items()
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _public_products_query(
    db: Session,
    category_id: str | None,
    search: str | None,
    seller_id: str | None,
) -> list[Product]:
    query = db.query(Product).filter(
        Product.status == "MODERATED",
        Product.deleted.is_(False),
        Product.skus.any(SKU.active_quantity > 0),
    )
    if category_id is not None:
        query = query.filter(
            Product.category_id == _normalize_uuid(category_id, "category_id")
        )
    if seller_id is not None:
        query = query.filter(Product.seller_id == _normalize_uuid(seller_id, "seller_id"))
    if search is not None and search.strip():
        escaped_search = _escape_like(search.strip())
        pattern = f"%{escaped_search}%"
        query = query.filter(
            Product.title.ilike(pattern, escape="\\")
            | Product.description.ilike(pattern, escape="\\")
        )
    return query.all()


@router.get("/products")
def list_public_products(
    request: Request,
    db: Session = Depends(get_db),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
    category_id: str | None = None,
    search: str | None = None,
    min_price: int | None = Query(default=None, ge=0),
    max_price: int | None = Query(default=None, ge=0),
    seller_id: str | None = None,
    sort: str = "created_desc",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    _require_service_key(x_service_key)
    if sort not in PUBLIC_SORTS:
        raise api_error(400, "INVALID_REQUEST", "sort is invalid")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise api_error(400, "INVALID_REQUEST", "min_price cannot exceed max_price")

    products = _public_products_query(db, category_id, search, seller_id)
    filters = _parse_deep_filters(request)
    products = [product for product in products if _matches_filters(product, filters)]
    if min_price is not None:
        products = [product for product in products if _minimum_price(product) >= min_price]
    if max_price is not None:
        products = [product for product in products if _minimum_price(product) <= max_price]

    if sort == "price_asc":
        products.sort(key=lambda product: (_minimum_price(product), product.id))
    elif sort == "price_desc":
        products.sort(key=lambda product: (_minimum_price(product), product.id), reverse=True)
    else:
        products.sort(key=lambda product: (product.created_at, product.id), reverse=True)

    total_count = len(products)
    page = products[offset : offset + limit]
    return {
        "items": [
            _serialize_public_product(product, include_out_of_stock_skus=False)
            for product in page
        ],
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }


@router.post("/products/batch")
def batch_public_products(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
) -> list[dict[str, Any]]:
    _require_service_key(x_service_key)
    raw_product_ids = payload.get("product_ids")
    if not isinstance(raw_product_ids, list):
        raise api_error(400, "INVALID_REQUEST", "product_ids must be an array")
    if len(raw_product_ids) > 100:
        raise api_error(400, "INVALID_REQUEST", "product_ids must contain at most 100 items")

    product_ids = [
        _normalize_uuid(product_id, "product_ids")
        for product_id in raw_product_ids
        if isinstance(product_id, str)
    ]
    if len(product_ids) != len(raw_product_ids):
        raise api_error(400, "INVALID_REQUEST", "product_ids must contain valid UUIDs")

    products = db.query(Product).filter(Product.id.in_(product_ids)).all()
    products_by_id = {
        product.id: product
        for product in products
        if _is_public_product(product)
    }
    return [
        _serialize_public_product(
            products_by_id[product_id],
            include_out_of_stock_skus=True,
        )
        for product_id in product_ids
        if product_id in products_by_id
    ]


@router.post("/skus/batch")
def batch_public_skus(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
) -> list[dict[str, Any]]:
    _require_service_key(x_service_key)
    raw_sku_ids = payload.get("sku_ids")
    if not isinstance(raw_sku_ids, list):
        raise api_error(400, "INVALID_REQUEST", "sku_ids must be an array")
    if len(raw_sku_ids) > 100:
        raise api_error(400, "INVALID_REQUEST", "sku_ids must contain at most 100 items")

    sku_ids = [
        _normalize_uuid(sku_id, "sku_ids")
        for sku_id in raw_sku_ids
        if isinstance(sku_id, str)
    ]
    if len(sku_ids) != len(raw_sku_ids):
        raise api_error(400, "INVALID_REQUEST", "sku_ids must contain valid UUIDs")

    skus = db.query(SKU).filter(SKU.id.in_(sku_ids)).all()
    skus_by_id = {sku.id: sku for sku in skus}
    return [
        _serialize_public_sku_lookup(skus_by_id[sku_id])
        for sku_id in sku_ids
        if sku_id in skus_by_id
    ]


@router.get("/skus/{sku_id}")
def get_public_sku(
    sku_id: str,
    db: Session = Depends(get_db),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
) -> dict[str, Any]:
    _require_service_key(x_service_key)
    normalized_sku_id = _normalize_uuid(sku_id, "sku_id")
    sku = db.get(SKU, normalized_sku_id)
    if sku is None or not _is_public_product_content(sku.product):
        raise api_error(404, "NOT_FOUND", "SKU not found")
    return _serialize_public_sku_lookup(sku)


@router.get("/products/{product_id}")
def get_public_product(
    product_id: str,
    db: Session = Depends(get_db),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
) -> dict[str, Any]:
    _require_service_key(x_service_key)
    normalized_product_id = _normalize_uuid(product_id, "product_id")
    product = db.get(Product, normalized_product_id)
    if product is None or not _is_public_product(product):
        raise api_error(404, "NOT_FOUND", "Product not found")
    return _serialize_public_product(product, include_out_of_stock_skus=True)
