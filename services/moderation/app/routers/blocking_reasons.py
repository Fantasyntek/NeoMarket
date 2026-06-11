from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import CurrentModerator, require_admin, require_moderator
from app.database import get_db
from app.errors import api_error
from app.models import BlockingReason


router = APIRouter(prefix="/api/v1", tags=["BlockingReasons"])
CODE_PATTERN = re.compile(r"^[A-Z_]+$")


def _serialize(reason: BlockingReason) -> dict[str, Any]:
    return {
        "id": reason.id,
        "code": reason.code,
        "title": reason.title,
        "description": reason.description,
        "hard_block": reason.hard_block,
        "is_active": reason.is_active,
    }


def _serialize_canonical(reason: BlockingReason) -> dict[str, Any]:
    return {
        "id": reason.id,
        "title": reason.title,
        "hard_block": reason.hard_block,
    }


def _reason_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise api_error(404, "BLOCKING_REASON_NOT_FOUND", "Blocking reason not found")


def _get_reason(db: Session, reason_id: str) -> BlockingReason:
    reason = db.get(BlockingReason, _reason_id(reason_id))
    if reason is None:
        raise api_error(404, "BLOCKING_REASON_NOT_FOUND", "Blocking reason not found")
    return reason


def _list_reasons(
    db: Session,
    *,
    hard_block: bool | None,
    is_active: bool,
) -> list[BlockingReason]:
    query = db.query(BlockingReason).filter(BlockingReason.is_active.is_(is_active))
    if hard_block is not None:
        query = query.filter(BlockingReason.hard_block.is_(hard_block))
    return query.order_by(BlockingReason.title, BlockingReason.id).all()


def _required_string(
    payload: dict[str, Any],
    field_name: str,
    *,
    max_length: int,
) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise api_error(400, "INVALID_REQUEST", f"{field_name} is required")
    value = value.strip()
    if len(value) > max_length:
        raise api_error(400, "INVALID_REQUEST", f"{field_name} is too long")
    return value


def _optional_description(payload: dict[str, Any]) -> str | None:
    value = payload.get("description")
    if value is None:
        return None
    if not isinstance(value, str):
        raise api_error(400, "INVALID_REQUEST", "description must be a string")
    value = value.strip()
    if len(value) > 2000:
        raise api_error(400, "INVALID_REQUEST", "description is too long")
    return value or None


@router.get("/product-blocking-reasons")
def list_product_blocking_reasons(
    hard_block: bool | None = Query(default=None),
    _: CurrentModerator = Depends(require_moderator),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return [
        _serialize_canonical(reason)
        for reason in _list_reasons(
            db,
            hard_block=hard_block,
            is_active=True,
        )
    ]


@router.get("/blocking-reasons")
def list_blocking_reasons_openapi(
    hard_block: bool | None = Query(default=None),
    is_active: bool = Query(default=True),
    _: CurrentModerator = Depends(require_moderator),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return [
        _serialize(reason)
        for reason in _list_reasons(
            db,
            hard_block=hard_block,
            is_active=is_active,
        )
    ]


@router.post("/blocking-reasons", status_code=201)
def create_blocking_reason(
    payload: dict[str, Any] = Body(...),
    _: CurrentModerator = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    code = _required_string(payload, "code", max_length=64)
    if CODE_PATTERN.fullmatch(code) is None:
        raise api_error(
            400,
            "INVALID_REQUEST",
            "code must contain only uppercase letters and underscores",
        )
    hard_block = payload.get("hard_block")
    if not isinstance(hard_block, bool):
        raise api_error(400, "INVALID_REQUEST", "hard_block must be a boolean")

    reason = BlockingReason(
        code=code,
        title=_required_string(payload, "title", max_length=200),
        description=_optional_description(payload),
        hard_block=hard_block,
        is_active=True,
    )
    db.add(reason)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(
            409,
            "BLOCKING_REASON_CODE_EXISTS",
            "Blocking reason code already exists",
        )
    db.refresh(reason)
    return _serialize(reason)


@router.patch("/blocking-reasons/{reason_id}")
def update_blocking_reason(
    reason_id: str,
    payload: dict[str, Any] = Body(...),
    _: CurrentModerator = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    reason = _get_reason(db, reason_id)
    if "title" in payload:
        reason.title = _required_string(payload, "title", max_length=200)
    if "description" in payload:
        reason.description = _optional_description(payload)
    if "is_active" in payload:
        if not isinstance(payload["is_active"], bool):
            raise api_error(400, "INVALID_REQUEST", "is_active must be a boolean")
        reason.is_active = payload["is_active"]
    db.commit()
    db.refresh(reason)
    return _serialize(reason)


@router.delete("/blocking-reasons/{reason_id}", status_code=204)
def deactivate_blocking_reason(
    reason_id: str,
    _: CurrentModerator = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    reason = _get_reason(db, reason_id)
    reason.is_active = False
    db.commit()
    return Response(status_code=204)
