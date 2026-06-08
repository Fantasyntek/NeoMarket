from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from app.auth import CurrentModerator, require_moderator
from app.b2b import (
    B2BDispatcher,
    build_moderated_event,
    dispatch_b2b_and_mark_sent,
    get_b2b_dispatcher,
    record_b2b_outbox_event,
    utc_now,
)
from app.database import get_db
from app.errors import api_error
from app.models import ProductModeration
from app.routers.queue import _serialize


router = APIRouter(prefix="/api/v1", tags=["Tickets"])


def _ticket_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise api_error(404, "TICKET_NOT_FOUND", "Ticket not found")


def _comment(payload: dict[str, Any] | None) -> str | None:
    if not payload or payload.get("comment") is None:
        return None
    value = payload["comment"]
    if not isinstance(value, str):
        raise api_error(400, "INVALID_REQUEST", "comment must be a string")
    value = value.strip()
    if len(value) > 2000:
        raise api_error(400, "INVALID_REQUEST", "comment is too long")
    return value or None


def _has_sku(card: ProductModeration) -> bool:
    snapshot = card.json_after or {}
    skus = snapshot.get("skus")
    return isinstance(skus, list) and any(isinstance(sku, dict) for sku in skus)


def _approve(
    ticket_id: str,
    payload: dict[str, Any] | None,
    moderator: CurrentModerator,
    db: Session,
    dispatcher: B2BDispatcher,
) -> dict[str, Any]:
    card = db.get(ProductModeration, _ticket_id(ticket_id))
    if card is None or card.archived:
        raise api_error(404, "TICKET_NOT_FOUND", "Ticket not found")
    if card.moderator_id != moderator.moderator_id:
        raise api_error(
            403,
            "CARD_OWNED_BY_ANOTHER_MODERATOR",
            "Card is assigned to another moderator",
        )
    if card.status != "IN_REVIEW":
        raise api_error(
            409,
            "INVALID_TICKET_STATUS",
            "Only an IN_REVIEW card can be approved",
        )
    if card.review_revision != card.content_revision:
        raise api_error(
            409,
            "PRODUCT_EDITED_DURING_REVIEW",
            "Product was edited during review",
        )
    if not _has_sku(card):
        raise api_error(
            409,
            "PRODUCT_HAS_NO_SKU",
            "Product without SKU cannot be approved",
        )

    decision_at = utc_now()
    card.status = "MODERATED"
    card.moderator_comment = _comment(payload)
    card.decision_at = decision_at
    card.claim_expires_at = None

    event_payload = build_moderated_event(
        card.product_id,
        moderator.moderator_id,
        decision_at,
    )
    outbox_event = record_b2b_outbox_event(db, event_payload)
    db.commit()
    db.refresh(card)
    dispatch_b2b_and_mark_sent(db, dispatcher, event_payload, outbox_event)
    return _serialize(card)


@router.post("/tickets/{ticket_id}/approve")
def approve_ticket_openapi(
    ticket_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    moderator: CurrentModerator = Depends(require_moderator),
    db: Session = Depends(get_db),
    dispatcher: B2BDispatcher = Depends(get_b2b_dispatcher),
) -> dict[str, Any]:
    return _approve(ticket_id, payload, moderator, db, dispatcher)


@router.post("/product-moderation/{ticket_id}/approve")
def approve_ticket_canonical(
    ticket_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    moderator: CurrentModerator = Depends(require_moderator),
    db: Session = Depends(get_db),
    dispatcher: B2BDispatcher = Depends(get_b2b_dispatcher),
) -> dict[str, Any]:
    return _approve(ticket_id, payload, moderator, db, dispatcher)
