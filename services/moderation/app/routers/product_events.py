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
from app.models import ModerationFieldReport, ProcessedProductEvent, ProductModeration


router = APIRouter(prefix="/api/v1", tags=["B2B Events"])

EVENT_TYPE_BY_CANON_ACTION = {
    "CREATED": "PRODUCT_CREATED",
    "EDITED": "PRODUCT_EDITED",
    "DELETED": "PRODUCT_DELETED",
}
SUPPORTED_EVENT_TYPES = set(EVENT_TYPE_BY_CANON_ACTION.values())


@dataclass(frozen=True)
class ProductEvent:
    idempotency_key: str
    event_type: str
    occurred_at: datetime
    product_id: str
    seller_id: str | None
    category_id: str | None
    queue_priority: int
    json_before: dict[str, Any] | None
    json_after: dict[str, Any] | None


def _require_service_key(value: str | None) -> None:
    if not is_valid_b2b_service_key(value):
        raise api_error(401, "UNAUTHORIZED", "Invalid service key")


def _uuid(value: Any, field_name: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise api_error(400, "INVALID_REQUEST", f"{field_name} is required")
    try:
        return str(UUID(value))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", f"{field_name} must be a valid UUID")


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise api_error(400, "INVALID_REQUEST", "occurred_at is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise api_error(
            400,
            "INVALID_REQUEST",
            "occurred_at must be an ISO 8601 timestamp",
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _snapshot(value: Any, field_name: str, *, required: bool) -> dict[str, Any] | None:
    if value is None and not required:
        return None
    if not isinstance(value, dict):
        raise api_error(400, "INVALID_REQUEST", f"{field_name} must be an object")
    return value


def _event_type(payload: dict[str, Any]) -> str:
    raw_value = payload.get("event_type", payload.get("event"))
    event_type = EVENT_TYPE_BY_CANON_ACTION.get(raw_value, raw_value)
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise api_error(
            400,
            "UNSUPPORTED_EVENT",
            "event must be CREATED, EDITED, or DELETED",
        )
    return str(event_type)


def _normalize_event(payload: dict[str, Any]) -> ProductEvent:
    event_type = _event_type(payload)
    nested = payload.get("payload")
    event_payload = nested if isinstance(nested, dict) else payload
    product_id = _uuid(event_payload.get("product_id"), "product_id")
    seller_id = _uuid(
        event_payload.get("seller_id"),
        "seller_id",
        required=event_type != "PRODUCT_DELETED",
    )
    category_id = _uuid(
        event_payload.get("category_id"),
        "category_id",
        required=False,
    )
    queue_priority = event_payload.get("queue_priority", 1)
    if (
        not isinstance(queue_priority, int)
        or isinstance(queue_priority, bool)
        or not 1 <= queue_priority <= 4
    ):
        raise api_error(
            400,
            "INVALID_REQUEST",
            "queue_priority must be between 1 and 4",
        )

    return ProductEvent(
        idempotency_key=_uuid(
            payload.get("idempotency_key"),
            "idempotency_key",
        ),
        event_type=event_type,
        occurred_at=_timestamp(payload.get("occurred_at", payload.get("date"))),
        product_id=product_id,
        seller_id=seller_id,
        category_id=category_id,
        queue_priority=queue_priority,
        json_before=_snapshot(
            event_payload.get("json_before"),
            "json_before",
            required=event_type == "PRODUCT_EDITED",
        ),
        json_after=_snapshot(
            event_payload.get("json_after"),
            "json_after",
            required=event_type != "PRODUCT_DELETED",
        ),
    )


def _total_active_quantity(snapshot: dict[str, Any] | None) -> int:
    if not snapshot:
        return 0
    return sum(
        max(
            int(
                sku.get(
                    "active_quantity",
                    sku.get("available_quantity", 0),
                )
            ),
            0,
        )
        for sku in snapshot.get("skus", [])
        if isinstance(sku, dict)
    )


def _created(db: Session, event: ProductEvent) -> ProductModeration:
    card = (
        db.query(ProductModeration)
        .filter(ProductModeration.product_id == event.product_id)
        .one_or_none()
    )
    if card is not None:
        if card.status == "HARD_BLOCKED":
            return card
        raise api_error(
            400,
            "PRODUCT_ALREADY_EXISTS",
            "Product moderation card already exists",
        )

    card = ProductModeration(
        product_id=event.product_id,
        seller_id=event.seller_id,
        category_id=event.category_id,
        kind="CREATE",
        status="PENDING",
        queue_priority=event.queue_priority,
        json_before=None,
        json_after=event.json_after,
        total_active_quantity=_total_active_quantity(event.json_after),
        content_revision=1,
    )
    db.add(card)
    return card


def _edited(db: Session, event: ProductEvent) -> ProductModeration:
    card = (
        db.query(ProductModeration)
        .filter(ProductModeration.product_id == event.product_id)
        .one_or_none()
    )
    if card is None or card.archived:
        raise api_error(
            400,
            "PRODUCT_NOT_FOUND",
            "Product moderation card does not exist",
        )
    if card.status == "HARD_BLOCKED":
        return card

    old_status = card.status
    card.kind = "EDIT"
    card.content_revision += 1
    card.seller_id = event.seller_id or card.seller_id
    card.category_id = event.category_id
    card.json_before = event.json_before or card.json_after
    card.json_after = event.json_after
    card.total_active_quantity = _total_active_quantity(event.json_after)
    card.blocking_reason_id = None
    card.moderator_comment = None
    db.query(ModerationFieldReport).filter(
        ModerationFieldReport.product_moderation_id == card.id
    ).delete(synchronize_session=False)

    if old_status == "IN_REVIEW":
        return card
    if old_status == "BLOCKED":
        card.queue_priority = 2
    elif old_status == "APPROVED":
        card.queue_priority = 3 if card.total_active_quantity > 0 else 4
    card.status = "PENDING"
    card.moderator_id = None
    return card


def _deleted(db: Session, event: ProductEvent) -> ProductModeration | None:
    card = (
        db.query(ProductModeration)
        .filter(ProductModeration.product_id == event.product_id)
        .one_or_none()
    )
    if card is None:
        return None
    if card.status == "HARD_BLOCKED":
        db.query(ModerationFieldReport).filter(
            ModerationFieldReport.product_moderation_id == card.id
        ).delete(synchronize_session=False)
        db.delete(card)
        return None
    card.status = "ARCHIVED"
    card.archived = True
    card.archived_at = event.occurred_at
    card.moderator_id = None
    return card


def _process_event(db: Session, event: ProductEvent) -> tuple[bool, ProductModeration | None]:
    if db.get(ProcessedProductEvent, event.idempotency_key) is not None:
        return True, None

    if event.event_type == "PRODUCT_CREATED":
        card = _created(db, event)
    elif event.event_type == "PRODUCT_EDITED":
        card = _edited(db, event)
    else:
        card = _deleted(db, event)

    db.add(
        ProcessedProductEvent(
            idempotency_key=event.idempotency_key,
            product_id=event.product_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return True, None
    if card is not None:
        db.refresh(card)
    return False, card


def _handle(
    payload: dict[str, Any],
    service_key: str | None,
    db: Session,
    *,
    status_code: int,
) -> JSONResponse:
    _require_service_key(service_key)
    duplicate, card = _process_event(db, _normalize_event(payload))
    return JSONResponse(
        status_code=status_code,
        content={
            "accepted": True,
            "duplicate": duplicate,
            "product_moderation_id": card.id if card else None,
            "status": card.status if card else None,
        },
    )


@router.post("/b2b/events", response_model=None)
def receive_openapi_product_event(
    payload: dict[str, Any],
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return _handle(payload, x_service_key, db, status_code=202)


@router.post("/events/product", response_model=None)
def receive_canonical_product_event(
    payload: dict[str, Any],
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return _handle(payload, x_service_key, db, status_code=200)
