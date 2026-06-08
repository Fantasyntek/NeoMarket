from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.b2b_client import (
    B2BClient,
    B2BResponseError,
    B2BUnavailableError,
    get_b2b_client,
)
from app.database import get_db
from app.errors import api_error
from app.models import Collection, CollectionProduct
from app.routers.catalog import _serialize_product_card


router = APIRouter(prefix="/api/v1", tags=["Collections"])


def _serialize_collection(collection: Collection) -> dict[str, Any]:
    return {
        "id": collection.id,
        "title": collection.title,
        "name": collection.title,
        "description": collection.description,
        "cover_image_url": collection.cover_image_url,
        "target_url": collection.target_url,
        "priority": collection.priority,
        "start_date": (
            collection.start_date.isoformat() if collection.start_date else None
        ),
    }


def _active_collections_query(db: Session) -> Any:
    return db.query(Collection).filter(
        Collection.is_active.is_(True),
        or_(Collection.start_date.is_(None), Collection.start_date <= date.today()),
    )


def _collections_page(
    db: Session,
    limit: int,
    offset: int,
) -> tuple[list[Collection], int]:
    query = _active_collections_query(db)
    total_count = query.count()
    collections = (
        query.order_by(Collection.priority, Collection.created_at, Collection.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return collections, total_count


@router.get("/main/collections")
def list_main_collections(
    db: Session = Depends(get_db),
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    collections, total_count = _collections_page(db, limit, offset)
    items = [_serialize_collection(collection) for collection in collections]
    return {
        "items": items,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "collections": items,
        "metadata": {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
        },
    }


@router.get("/catalog/collections")
def list_catalog_collections(
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    collections, _ = _collections_page(db, limit=100, offset=0)
    return [
        {**_serialize_collection(collection), "products": []}
        for collection in collections
    ]


def _normalize_collection_id(collection_id: str) -> str:
    try:
        return str(UUID(collection_id))
    except ValueError:
        raise api_error(
            400,
            "INVALID_REQUEST",
            "collection_id must be a valid UUID",
        )


def _batch_products(
    b2b_client: B2BClient,
    product_ids: list[str],
) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    try:
        for start in range(0, len(product_ids), 100):
            products.extend(
                b2b_client.batch_products(product_ids[start : start + 100])
            )
    except B2BUnavailableError:
        raise api_error(503, "B2B_UNAVAILABLE", "Catalog is temporarily unavailable")
    except B2BResponseError as exc:
        raise api_error(
            exc.status_code,
            str(exc.payload.get("code", "B2B_ERROR")),
            str(exc.payload.get("message", "B2B request failed")),
        )
    return products


@router.get("/collections/{collection_id}/products")
def get_collection_products(
    collection_id: str,
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    normalized_collection_id = _normalize_collection_id(collection_id)
    collection = (
        _active_collections_query(db)
        .filter(Collection.id == normalized_collection_id)
        .one_or_none()
    )
    if collection is None:
        raise api_error(404, "COLLECTION_NOT_FOUND", "Collection not found")

    links_query = db.query(CollectionProduct).filter(
        CollectionProduct.collection_id == normalized_collection_id
    )
    total_products = links_query.count()
    links = (
        links_query.order_by(CollectionProduct.ordering, CollectionProduct.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    product_ids = [link.product_id for link in links]
    products = _batch_products(b2b_client, product_ids) if product_ids else []
    products_by_id = {str(product["id"]): product for product in products}
    items = [
        _serialize_product_card(products_by_id[product_id])
        for product_id in product_ids
        if product_id in products_by_id
    ]
    unavailable_ids = [
        product_id for product_id in product_ids if product_id not in products_by_id
    ]
    return {
        "collection_id": collection.id,
        "collection_title": collection.title,
        "items": items,
        "unavailable_ids": unavailable_ids,
        "total_products": total_products,
        "limit": limit,
        "offset": offset,
    }
