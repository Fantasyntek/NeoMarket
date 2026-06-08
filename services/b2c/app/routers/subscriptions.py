from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response
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
from app.models import ProductSubscription


router = APIRouter(prefix="/api/v1/favorites", tags=["Favorites"])
ALLOWED_NOTIFICATION_EVENTS = {"IN_STOCK", "PRICE_DOWN"}
LEGACY_EVENT_ALIASES = {
    "BACK_IN_STOCK": "IN_STOCK",
    "PRICE_DROP": "PRICE_DOWN",
}


def _normalize_product_id(product_id: str) -> str:
    try:
        return str(UUID(product_id))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", "product_id must be a valid UUID")


def _validate_notify_on(payload: dict[str, Any]) -> list[str]:
    notify_on = payload.get("notify_on", payload.get("events"))
    if not isinstance(notify_on, list) or not notify_on:
        raise api_error(
            400,
            "INVALID_NOTIFY_ON",
            "notify_on must be a non-empty array",
        )
    normalized_events = [
        LEGACY_EVENT_ALIASES.get(event, event) if isinstance(event, str) else event
        for event in notify_on
    ]
    if any(
        not isinstance(event, str) or event not in ALLOWED_NOTIFICATION_EVENTS
        for event in normalized_events
    ):
        allowed = ", ".join(sorted(ALLOWED_NOTIFICATION_EVENTS))
        raise api_error(
            400,
            "INVALID_NOTIFY_ON",
            f"notify_on values must be one of: {allowed}",
        )
    return list(dict.fromkeys(normalized_events))


def _require_visible_product(b2b_client: B2BClient, product_id: str) -> None:
    try:
        b2b_client.get_product(product_id)
    except B2BUnavailableError:
        raise api_error(503, "B2B_UNAVAILABLE", "Catalog is temporarily unavailable")
    except B2BResponseError as exc:
        if exc.status_code == 404:
            raise api_error(404, "PRODUCT_NOT_FOUND", "Product not found")
        raise api_error(
            exc.status_code,
            str(exc.payload.get("code", "B2B_ERROR")),
            str(exc.payload.get("message", "B2B request failed")),
        )


def _subscription_response(subscription: ProductSubscription) -> dict[str, Any]:
    return {
        "id": subscription.id,
        "product_id": subscription.product_id,
        "notify_on": subscription.notify_on,
        "created_at": subscription.created_at.isoformat(),
    }


@router.post("/{product_id}/subscribe")
def subscribe_to_product(
    product_id: str,
    payload: dict[str, Any],
    current_user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
) -> JSONResponse:
    normalized_product_id = _normalize_product_id(product_id)
    notify_on = _validate_notify_on(payload)
    existing = (
        db.query(ProductSubscription)
        .filter(
            ProductSubscription.user_id == current_user.user_id,
            ProductSubscription.product_id == normalized_product_id,
        )
        .one_or_none()
    )
    if existing is not None:
        raise api_error(
            409,
            "SUBSCRIPTION_ALREADY_EXISTS",
            "Subscription already exists",
        )

    _require_visible_product(b2b_client, normalized_product_id)
    subscription = ProductSubscription(
        user_id=current_user.user_id,
        product_id=normalized_product_id,
        notify_on=notify_on,
    )
    db.add(subscription)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(
            409,
            "SUBSCRIPTION_ALREADY_EXISTS",
            "Subscription already exists",
        )
    db.refresh(subscription)
    return JSONResponse(
        status_code=201,
        content=_subscription_response(subscription),
    )


@router.delete("/{product_id}/subscribe", status_code=204)
def unsubscribe_from_product(
    product_id: str,
    current_user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    normalized_product_id = _normalize_product_id(product_id)
    subscription = (
        db.query(ProductSubscription)
        .filter(
            ProductSubscription.user_id == current_user.user_id,
            ProductSubscription.product_id == normalized_product_id,
        )
        .one_or_none()
    )
    if subscription is not None:
        db.delete(subscription)
        db.commit()
    return Response(status_code=204)
