from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import is_valid_b2b_service_key
from app.database import get_db
from app.errors import api_error
from app.models import CartItem, ProcessedB2BEvent


router = APIRouter(prefix="/api/v1", tags=["B2B Events"])

REASON_BY_EVENT = {
    "PRODUCT_BLOCKED": "PRODUCT_BLOCKED",
    "PRODUCT_HARD_BLOCKED": "PRODUCT_BLOCKED",
    "PRODUCT_DELETED": "PRODUCT_DELETED",
    "SKU_OUT_OF_STOCK": "OUT_OF_STOCK",
}


@dataclass(frozen=True)
class ProductEvent:
    idempotency_key: str
    event_type: str
    occurred_at: datetime
    product_id: str
    sku_ids: list[str]


def _require_service_key(value: str | None) -> None:
    if not is_valid_b2b_service_key(value):
        raise api_error(401, "UNAUTHORIZED", "Invalid service key")


def _uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise api_error(400, "INVALID_REQUEST", f"{field_name} is required")
    try:
        return str(UUID(value))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", f"{field_name} must be a valid UUID")


def _timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise api_error(400, "INVALID_REQUEST", f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise api_error(
            400,
            "INVALID_REQUEST",
            f"{field_name} must be an ISO 8601 timestamp",
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sku_ids(raw_values: Any) -> list[str]:
    if raw_values is None:
        return []
    if not isinstance(raw_values, list):
        raise api_error(400, "INVALID_REQUEST", "sku_ids must be an array")
    return list(
        dict.fromkeys(
            _uuid(value, f"sku_ids[{index}]")
            for index, value in enumerate(raw_values)
        )
    )


def _normalize_event(payload: dict[str, Any]) -> ProductEvent:
    event_type = payload.get("event_type", payload.get("event"))
    if event_type not in REASON_BY_EVENT:
        raise api_error(
            400,
            "UNSUPPORTED_EVENT",
            "event type must be PRODUCT_BLOCKED, PRODUCT_DELETED, or "
            "SKU_OUT_OF_STOCK",
        )

    nested_payload = payload.get("payload")
    event_payload = nested_payload if isinstance(nested_payload, dict) else payload
    product_id = _uuid(event_payload.get("product_id"), "product_id")

    raw_sku_ids = event_payload.get("sku_ids")
    if raw_sku_ids is None:
        single_sku_id = event_payload.get("sku_id", payload.get("sku_id"))
        raw_sku_ids = [single_sku_id] if single_sku_id is not None else []

    return ProductEvent(
        idempotency_key=_uuid(payload.get("idempotency_key"), "idempotency_key"),
        event_type=str(event_type),
        occurred_at=_timestamp(
            payload.get("occurred_at", payload.get("date")),
            "occurred_at",
        ),
        product_id=product_id,
        sku_ids=_sku_ids(raw_sku_ids),
    )


def _process_event(
    db: Session,
    event: ProductEvent,
) -> tuple[bool, int]:
    if db.get(ProcessedB2BEvent, event.idempotency_key) is not None:
        return True, 0

    query = db.query(CartItem)
    if event.sku_ids:
        query = query.filter(CartItem.sku_id.in_(event.sku_ids))
    else:
        query = query.filter(CartItem.product_id == event.product_id)
    affected_count = query.update(
        {CartItem.unavailable_reason: REASON_BY_EVENT[event.event_type]},
        synchronize_session=False,
    )
    db.add(
        ProcessedB2BEvent(
            idempotency_key=event.idempotency_key,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return True, 0
    return False, affected_count


def _handle_event(
    payload: dict[str, Any],
    x_service_key: str | None,
    db: Session,
    *,
    status_code: int,
) -> JSONResponse:
    _require_service_key(x_service_key)
    duplicate, affected_count = _process_event(db, _normalize_event(payload))
    return JSONResponse(
        status_code=status_code,
        content={
            "accepted": True,
            "duplicate": duplicate,
            "affected_cart_items": affected_count,
        },
    )


@router.post("/events/product", response_model=None)
def receive_canonical_product_event(
    payload: dict[str, Any],
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return _handle_event(payload, x_service_key, db, status_code=200)


@router.post("/b2b/events", response_model=None)
def receive_openapi_b2b_event(
    payload: dict[str, Any],
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return _handle_event(payload, x_service_key, db, status_code=202)
