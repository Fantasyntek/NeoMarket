from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import CurrentUser, require_user
from app.b2b_client import (
    B2BClient,
    B2BResponseError,
    B2BUnavailableError,
    get_b2b_client,
)
from app.database import get_db
from app.errors import api_error
from app.models import Favorite
from app.routers.catalog import _serialize_product_card


router = APIRouter(prefix="/api/v1/favorites", tags=["Favorites"])


def _normalize_product_id(product_id: str) -> str:
    try:
        return str(UUID(product_id))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", "product_id must be a valid UUID")


def _product_not_found() -> None:
    raise api_error(404, "PRODUCT_NOT_FOUND", "Product not found")


def _favorite_response(favorite: Favorite, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "product_id": favorite.product_id,
            "added_at": favorite.added_at.isoformat(),
        },
    )


def _require_visible_product(b2b_client: B2BClient, product_id: str) -> None:
    try:
        b2b_client.get_product(product_id)
    except B2BUnavailableError:
        raise api_error(503, "B2B_UNAVAILABLE", "Catalog is temporarily unavailable")
    except B2BResponseError as exc:
        if exc.status_code == 404:
            _product_not_found()
        raise api_error(
            exc.status_code,
            str(exc.payload.get("code", "B2B_ERROR")),
            str(exc.payload.get("message", "B2B request failed")),
        )


def _batch_visible_products(
    b2b_client: B2BClient,
    product_ids: list[str],
) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    try:
        for start in range(0, len(product_ids), 100):
            products.extend(b2b_client.batch_products(product_ids[start : start + 100]))
    except B2BUnavailableError:
        raise api_error(503, "B2B_UNAVAILABLE", "Catalog is temporarily unavailable")
    except B2BResponseError as exc:
        raise api_error(
            exc.status_code,
            str(exc.payload.get("code", "B2B_ERROR")),
            str(exc.payload.get("message", "B2B request failed")),
        )
    return products


@router.post("/{product_id}")
def add_favorite(
    product_id: str,
    current_user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
) -> JSONResponse:
    normalized_product_id = _normalize_product_id(product_id)
    existing = (
        db.query(Favorite)
        .filter(
            Favorite.user_id == current_user.user_id,
            Favorite.product_id == normalized_product_id,
        )
        .one_or_none()
    )
    if existing is not None:
        return _favorite_response(existing, 200)

    _require_visible_product(b2b_client, normalized_product_id)
    favorite = Favorite(
        user_id=current_user.user_id,
        product_id=normalized_product_id,
    )
    db.add(favorite)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        favorite = (
            db.query(Favorite)
            .filter(
                Favorite.user_id == current_user.user_id,
                Favorite.product_id == normalized_product_id,
            )
            .one()
        )
        return _favorite_response(favorite, 200)
    db.refresh(favorite)
    return _favorite_response(favorite, 201)


@router.delete("/{product_id}", status_code=204)
def delete_favorite(
    product_id: str,
    current_user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    normalized_product_id = _normalize_product_id(product_id)
    favorite = (
        db.query(Favorite)
        .filter(
            Favorite.user_id == current_user.user_id,
            Favorite.product_id == normalized_product_id,
        )
        .one_or_none()
    )
    if favorite is not None:
        db.delete(favorite)
        db.commit()
    return Response(status_code=204)


@router.get("")
def list_favorites(
    current_user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    favorites = (
        db.query(Favorite)
        .filter(Favorite.user_id == current_user.user_id)
        .order_by(Favorite.added_at.desc(), Favorite.id.desc())
        .all()
    )
    if not favorites:
        return {"items": [], "total_count": 0, "limit": limit, "offset": offset}

    ordered_ids = [favorite.product_id for favorite in favorites]
    products = _batch_visible_products(b2b_client, ordered_ids)
    products_by_id = {str(product["id"]): product for product in products}
    visible_products = [
        products_by_id[product_id]
        for product_id in ordered_ids
        if product_id in products_by_id
    ]
    page = visible_products[offset : offset + limit]
    return {
        "items": [_serialize_product_card(product) for product in page],
        "total_count": len(visible_products),
        "limit": limit,
        "offset": offset,
    }
