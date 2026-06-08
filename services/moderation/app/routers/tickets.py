from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from app.auth import CurrentModerator, require_moderator
from app.b2b import (
    B2BDispatcher,
    build_blocked_event,
    build_moderated_event,
    dispatch_b2b_and_mark_sent,
    get_b2b_dispatcher,
    record_b2b_outbox_event,
    utc_now,
)
from app.database import get_db
from app.errors import api_error
from app.models import BlockingReason, ModerationFieldReport, ProductModeration
from app.routers.queue import _serialize


router = APIRouter(prefix="/api/v1", tags=["Tickets"])

ALLOWED_FIELD_NAMES = {
    "title",
    "description",
    "product_images",
    "category",
    "sku_name",
    "sku_image",
    "sku_price",
}


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


def _reason_id(payload: dict[str, Any] | None) -> str:
    payload = payload or {}
    raw_value = payload.get("blocking_reason_id")
    if raw_value is None:
        raw_values = payload.get("blocking_reason_ids")
        if not isinstance(raw_values, list) or len(raw_values) != 1:
            raise api_error(
                400,
                "INVALID_BLOCKING_REASON",
                "Exactly one blocking reason is required",
            )
        raw_value = raw_values[0]
    if not isinstance(raw_value, str):
        raise api_error(
            400,
            "INVALID_BLOCKING_REASON",
            "blocking_reason_id must be a valid UUID",
        )
    try:
        return str(UUID(raw_value))
    except ValueError:
        raise api_error(
            400,
            "INVALID_BLOCKING_REASON",
            "blocking_reason_id must be a valid UUID",
        )


def _normalize_field_name(field_name: str) -> str | None:
    if field_name in ALLOWED_FIELD_NAMES:
        return field_name
    if field_name == "category_id":
        return "category"
    if field_name.startswith("images"):
        return "product_images"
    if field_name.startswith("skus"):
        suffix = field_name.rsplit(".", 1)[-1]
        return {
            "name": "sku_name",
            "image": "sku_image",
            "price": "sku_price",
        }.get(suffix)
    return None


def _field_reports(
    payload: dict[str, Any] | None,
) -> list[dict[str, str | None]]:
    raw_reports = (payload or {}).get("field_reports", [])
    if not isinstance(raw_reports, list):
        raise api_error(400, "INVALID_FIELD_REPORT", "field_reports must be an array")

    reports: list[dict[str, str | None]] = []
    for index, raw_report in enumerate(raw_reports):
        if not isinstance(raw_report, dict):
            raise api_error(
                400,
                "INVALID_FIELD_REPORT",
                f"field_reports[{index}] must be an object",
            )
        field_name = raw_report.get("field_name", raw_report.get("field_path"))
        comment = raw_report.get("comment", raw_report.get("message"))
        severity = raw_report.get("severity", "ERROR")
        sku_id = raw_report.get("sku_id")
        normalized_field_name = (
            _normalize_field_name(field_name.strip())
            if isinstance(field_name, str)
            else None
        )
        if normalized_field_name is None:
            raise api_error(
                400,
                "INVALID_FIELD_NAME",
                f"field_reports[{index}].field_name is not allowed",
            )
        if not isinstance(comment, str) or not comment.strip():
            raise api_error(
                400,
                "INVALID_FIELD_REPORT",
                f"field_reports[{index}].comment is required",
            )
        if len(comment.strip()) > 1000:
            raise api_error(
                400,
                "INVALID_FIELD_REPORT",
                f"field_reports[{index}].comment is too long",
            )
        if severity not in {"INFO", "WARNING", "ERROR"}:
            raise api_error(
                400,
                "INVALID_FIELD_REPORT",
                f"field_reports[{index}].severity is invalid",
            )
        if sku_id is not None:
            if not isinstance(sku_id, str):
                raise api_error(
                    400,
                    "INVALID_FIELD_REPORT",
                    f"field_reports[{index}].sku_id must be a valid UUID",
                )
            try:
                sku_id = str(UUID(sku_id))
            except ValueError:
                raise api_error(
                    400,
                    "INVALID_FIELD_REPORT",
                    f"field_reports[{index}].sku_id must be a valid UUID",
                )
        reports.append(
            {
                "field_name": normalized_field_name,
                "sku_id": sku_id,
                "comment": comment.strip(),
                "severity": severity,
            }
        )
    return reports


def _owned_in_review_card(
    ticket_id: str,
    moderator: CurrentModerator,
    db: Session,
) -> ProductModeration:
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
            "Only an IN_REVIEW card can receive a decision",
        )
    if card.review_revision != card.content_revision:
        raise api_error(
            409,
            "PRODUCT_EDITED_DURING_REVIEW",
            "Product was edited during review",
        )
    return card


def _owned_in_review_product(
    product_id: str,
    moderator: CurrentModerator,
    db: Session,
) -> ProductModeration:
    try:
        normalized_product_id = str(UUID(product_id))
    except ValueError:
        raise api_error(404, "TICKET_NOT_FOUND", "Ticket not found")
    card = (
        db.query(ProductModeration)
        .filter(ProductModeration.product_id == normalized_product_id)
        .one_or_none()
    )
    if card is None:
        raise api_error(404, "TICKET_NOT_FOUND", "Ticket not found")
    return _owned_in_review_card(card.id, moderator, db)


def _approve(
    ticket_id: str,
    payload: dict[str, Any] | None,
    moderator: CurrentModerator,
    db: Session,
    dispatcher: B2BDispatcher,
) -> dict[str, Any]:
    card = _owned_in_review_card(ticket_id, moderator, db)
    if not _has_sku(card):
        raise api_error(
            409,
            "PRODUCT_HAS_NO_SKU",
            "Product without SKU cannot be approved",
        )

    decision_at = utc_now()
    card.status = "MODERATED"
    card.moderator_comment = _comment(payload)
    card.blocking_reason_id = None
    card.decision_at = decision_at
    card.claim_expires_at = None
    db.query(ModerationFieldReport).filter(
        ModerationFieldReport.product_moderation_id == card.id
    ).delete(synchronize_session=False)

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


def _soft_block(
    ticket_id: str,
    payload: dict[str, Any] | None,
    moderator: CurrentModerator,
    db: Session,
    dispatcher: B2BDispatcher,
) -> dict[str, Any]:
    card = _owned_in_review_card(ticket_id, moderator, db)
    blocking_reason_id = _reason_id(payload)
    reason = db.get(BlockingReason, blocking_reason_id)
    if reason is None or not reason.is_active:
        raise api_error(
            400,
            "UNKNOWN_BLOCKING_REASON",
            "Blocking reason does not exist or is inactive",
        )
    if reason.hard_block:
        raise api_error(
            400,
            "HARD_BLOCK_REASON_NOT_ALLOWED",
            "Hard-block reason cannot be used for a soft block",
        )

    reports = _field_reports(payload)
    moderator_comment = _comment(
        {
            "comment": (payload or {}).get(
                "moderator_comment",
                (payload or {}).get("comment"),
            )
        }
    )
    decision_at = utc_now()
    card.status = "BLOCKED"
    card.blocking_reason_id = reason.id
    card.moderator_comment = moderator_comment
    card.decision_at = decision_at
    card.claim_expires_at = None

    db.query(ModerationFieldReport).filter(
        ModerationFieldReport.product_moderation_id == card.id
    ).delete(synchronize_session=False)
    for report in reports:
        db.add(
            ModerationFieldReport(
                product_moderation_id=card.id,
                field_name=report["field_name"] or "",
                sku_id=report["sku_id"],
                comment=report["comment"] or "",
                severity=report["severity"] or "ERROR",
            )
        )

    event_reports = [
        {
            "field_name": report["field_name"],
            "sku_id": report["sku_id"],
            "comment": report["comment"],
        }
        for report in reports
    ]
    event_payload = build_blocked_event(
        card.product_id,
        moderator.moderator_id,
        reason.id,
        moderator_comment,
        event_reports,
        decision_at,
    )
    outbox_event = record_b2b_outbox_event(db, event_payload)
    db.commit()
    db.refresh(card)
    dispatch_b2b_and_mark_sent(db, dispatcher, event_payload, outbox_event)
    return _serialize(card)


def _soft_block_product(
    product_id: str,
    payload: dict[str, Any] | None,
    moderator: CurrentModerator,
    db: Session,
    dispatcher: B2BDispatcher,
) -> dict[str, Any]:
    card = _owned_in_review_product(product_id, moderator, db)
    return _soft_block(card.id, payload, moderator, db, dispatcher)


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


@router.post("/tickets/{ticket_id}/block")
def block_ticket_openapi(
    ticket_id: str,
    payload: dict[str, Any],
    moderator: CurrentModerator = Depends(require_moderator),
    db: Session = Depends(get_db),
    dispatcher: B2BDispatcher = Depends(get_b2b_dispatcher),
) -> dict[str, Any]:
    return _soft_block(ticket_id, payload, moderator, db, dispatcher)


@router.post("/products/{product_id}/decline")
def block_ticket_canonical(
    product_id: str,
    payload: dict[str, Any],
    moderator: CurrentModerator = Depends(require_moderator),
    db: Session = Depends(get_db),
    dispatcher: B2BDispatcher = Depends(get_b2b_dispatcher),
) -> dict[str, Any]:
    return _soft_block_product(product_id, payload, moderator, db, dispatcher)
