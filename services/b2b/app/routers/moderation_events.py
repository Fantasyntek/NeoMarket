from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
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


def _require_event_type(payload: dict[str, Any]) -> str:
    event_type = payload.get("event_type")
    if event_type not in {"MODERATED", "BLOCKED"}:
        raise api_error(
            400,
            "INVALID_REQUEST",
            "event_type must be MODERATED or BLOCKED",
        )
    return event_type


def _require_occurred_at(payload: dict[str, Any]) -> datetime:
    value = payload.get("occurred_at")
    if not isinstance(value, str) or not value.strip():
        raise api_error(400, "INVALID_REQUEST", "occurred_at is required")
    try:
        occurred_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", "occurred_at must be a valid date-time")
    if occurred_at.tzinfo is None:
        raise api_error(400, "INVALID_REQUEST", "occurred_at must include a timezone")
    return occurred_at


def _optional_string(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise api_error(400, "INVALID_REQUEST", f"{field} must be a string")
    return value.strip()


def _optional_boolean(payload: dict[str, Any], field: str, default: bool) -> bool:
    value = payload.get(field, default)
    if not isinstance(value, bool):
        raise api_error(400, "INVALID_REQUEST", f"{field} must be a boolean")
    return value


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


@router.post("/moderation/events", status_code=204)
def apply_moderation_event(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    b2c_dispatcher: B2CDispatcher = Depends(get_b2c_dispatcher),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
) -> Response:
    _require_service_key(x_service_key)
    idempotency_key = _require_uuid(payload, "idempotency_key")
    product_id = _require_uuid(payload, "product_id")
    event_type = _require_event_type(payload)
    _require_occurred_at(payload)

    hard_block = False
    blocking_reason_id = None
    moderator_comment = None
    field_reports: list[dict[str, str | None]] = []
    if event_type == "BLOCKED":
        blocking_reason_id = _require_uuid(payload, "blocking_reason_id")
        moderator_comment = _optional_string(payload, "moderator_comment")
        hard_block = _optional_boolean(payload, "hard_block", False)
        field_reports = _validate_field_reports(payload)

    if db.get(ProcessedModerationEvent, idempotency_key) is not None:
        return Response(status_code=204)

    product = db.get(Product, product_id)
    if product is None:
        raise api_error(404, "NOT_FOUND", "Product not found")

    b2c_payload: dict[str, Any] | None = None
    b2c_outbox_event = None
    if event_type == "MODERATED":
        product.status = "MODERATED"
        product.blocked = False
        product.blocking_reason_id = None
        product.blocking_reason_title = None
        product.moderator_comment = None
        product.field_reports = []
    else:
        product.status = "HARD_BLOCKED" if hard_block else "BLOCKED"
        product.blocked = True
        product.blocking_reason_id = blocking_reason_id
        product.blocking_reason_title = None
        product.moderator_comment = moderator_comment
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
    return Response(status_code=204)
