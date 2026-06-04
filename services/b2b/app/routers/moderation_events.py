from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.auth import is_valid_service_key
from app.b2c import (
    B2CDispatcher,
    build_product_blocked_event,
    dispatch_b2c_and_mark_sent,
    get_b2c_dispatcher,
    record_b2c_outbox_event,
)
from app.database import get_db
from app.errors import api_error
from app.models import ProcessedModerationEvent, Product, ProductFieldReport


router = APIRouter(prefix="/api/v1", tags=["Moderation Events"])


def _require_service_key(x_service_key: str | None) -> None:
    if not is_valid_service_key(x_service_key):
        raise api_error(401, "UNAUTHORIZED", "Invalid service key")


def _require_uuid(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise api_error(400, "INVALID_REQUEST", f"{field} is required")
    try:
        return str(UUID(value))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", f"{field} must be a valid UUID")


def _require_status(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    if status not in {"MODERATED", "BLOCKED"}:
        raise api_error(400, "INVALID_REQUEST", "status must be MODERATED or BLOCKED")
    return status


def _require_blocking_reason(payload: dict[str, Any]) -> dict[str, str]:
    reason = payload.get("blocking_reason")
    if not isinstance(reason, dict):
        raise api_error(400, "INVALID_REQUEST", "blocking_reason is required")

    reason_id = reason.get("id")
    if not isinstance(reason_id, str) or not reason_id.strip():
        raise api_error(400, "INVALID_REQUEST", "blocking_reason.id is required")
    try:
        reason_id = str(UUID(reason_id))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", "blocking_reason.id must be a valid UUID")
    title = reason.get("title")
    comment = reason.get("comment")
    if not isinstance(title, str) or not title.strip():
        raise api_error(400, "INVALID_REQUEST", "blocking_reason.title is required")
    if not isinstance(comment, str) or not comment.strip():
        raise api_error(400, "INVALID_REQUEST", "blocking_reason.comment is required")
    return {"id": reason_id, "title": title.strip(), "comment": comment.strip()}


def _validate_field_reports(payload: dict[str, Any]) -> list[dict[str, str | None]]:
    raw_reports = payload.get("field_reports", [])
    if raw_reports is None:
        return []
    if not isinstance(raw_reports, list):
        raise api_error(400, "INVALID_REQUEST", "field_reports must be an array")

    reports: list[dict[str, str | None]] = []
    for index, raw_report in enumerate(raw_reports):
        if not isinstance(raw_report, dict):
            raise api_error(400, "INVALID_REQUEST", f"field_reports[{index}] must be an object")
        field_name = raw_report.get("field_name")
        comment = raw_report.get("comment")
        sku_id = raw_report.get("sku_id")
        if not isinstance(field_name, str) or not field_name.strip():
            raise api_error(400, "INVALID_REQUEST", f"field_reports[{index}].field_name is required")
        if not isinstance(comment, str) or not comment.strip():
            raise api_error(400, "INVALID_REQUEST", f"field_reports[{index}].comment is required")
        if sku_id is not None:
            sku_id = _require_uuid(raw_report, "sku_id")
        reports.append(
            {
                "field_name": field_name.strip(),
                "sku_id": sku_id,
                "comment": comment.strip(),
            }
        )
    return reports


@router.post("/events/moderation")
def apply_moderation_event(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    b2c_dispatcher: B2CDispatcher = Depends(get_b2c_dispatcher),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
) -> dict[str, bool]:
    _require_service_key(x_service_key)
    idempotency_key = _require_uuid(payload, "idempotency_key")
    if db.get(ProcessedModerationEvent, idempotency_key) is not None:
        return {"ok": True}

    product_id = _require_uuid(payload, "product_id")
    status = _require_status(payload)
    product = db.get(Product, product_id)
    if product is None:
        raise api_error(404, "NOT_FOUND", "Product not found")

    b2c_payload: dict[str, Any] | None = None
    b2c_outbox_event = None
    if status == "MODERATED":
        product.status = "MODERATED"
        product.blocked = False
        product.blocking_reason_id = None
        product.blocking_reason_title = None
        product.moderator_comment = None
        product.field_reports = []
    else:
        hard_block = bool(payload.get("hard_block", False))
        blocking_reason = _require_blocking_reason(payload)
        field_reports = _validate_field_reports(payload)

        product.status = "HARD_BLOCKED" if hard_block else "BLOCKED"
        product.blocked = True
        product.blocking_reason_id = blocking_reason["id"]
        product.blocking_reason_title = blocking_reason["title"]
        product.moderator_comment = blocking_reason["comment"]
        product.field_reports = [
            ProductFieldReport(
                field_name=report["field_name"] or "",
                sku_id=report["sku_id"],
                comment=report["comment"] or "",
            )
            for report in field_reports
        ]
        b2c_payload = build_product_blocked_event(
            product,
            [sku.id for sku in product.skus],
        )
        b2c_outbox_event = record_b2c_outbox_event(db, b2c_payload)

    db.add(
        ProcessedModerationEvent(
            idempotency_key=idempotency_key,
            product_id=product.id,
            status=product.status,
        )
    )
    db.commit()
    dispatch_b2c_and_mark_sent(db, b2c_dispatcher, b2c_payload, b2c_outbox_event)
    return {"ok": True}
