from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import is_valid_service_key
from app.b2c import (
    B2CDispatcher,
    build_sku_out_of_stock_event,
    dispatch_b2c_and_mark_sent,
    get_b2c_dispatcher,
    record_b2c_outbox_event,
)
from app.database import get_db
from app.errors import api_error
from app.models import FulfillOperation, ReserveOperation, SKU, UnreserveOperation


router = APIRouter(prefix="/api/v1", tags=["Inventory"])


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


def _require_positive_quantity(payload: dict[str, Any]) -> int:
    quantity = payload.get("quantity")
    if not isinstance(quantity, int) or quantity <= 0:
        raise api_error(400, "INVALID_REQUEST", "quantity must be > 0")
    return quantity


def _validate_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise api_error(400, "INVALID_REQUEST", "At least one item is required")

    items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise api_error(400, "INVALID_REQUEST", f"items[{index}] must be an object")
        items.append(
            {
                "sku_id": _require_uuid(raw_item, "sku_id"),
                "quantity": _require_positive_quantity(raw_item),
            }
        )
    return items


def _load_skus_for_update(db: Session, sku_ids: list[str]) -> dict[str, SKU]:
    return {
        sku.id: sku
        for sku in db.query(SKU)
        .filter(SKU.id.in_(sku_ids))
        .with_for_update()
        .all()
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reserve_failed_response(
    items: list[dict[str, Any]],
    skus_by_id: dict[str, SKU],
) -> JSONResponse:
    failed_items = []
    for item in items:
        sku = skus_by_id.get(item["sku_id"])
        available = sku.active_quantity if sku is not None else 0
        if sku is None or available < item["quantity"]:
            failed_items.append(
                {
                    "sku_id": item["sku_id"],
                    "requested": item["quantity"],
                    "available": available,
                    "reason": "OUT_OF_STOCK" if available == 0 else "INSUFFICIENT_STOCK",
                }
            )
    return JSONResponse(
        status_code=409,
        content={"reserved": False, "failed_items": failed_items},
    )


@router.post("/inventory/reserve", response_model=None)
def reserve_skus(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    b2c_dispatcher: B2CDispatcher = Depends(get_b2c_dispatcher),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
) -> dict[str, Any] | JSONResponse:
    _require_service_key(x_service_key)
    idempotency_key = _require_uuid(payload, "idempotency_key")
    order_id = _require_uuid(payload, "order_id")
    existing_operation = db.get(ReserveOperation, idempotency_key)
    if existing_operation is not None:
        return json.loads(existing_operation.result_json)

    items = _validate_items(payload)
    skus_by_id = _load_skus_for_update(db, [item["sku_id"] for item in items])
    if any(
        item["sku_id"] not in skus_by_id
        or skus_by_id[item["sku_id"]].active_quantity < item["quantity"]
        for item in items
    ):
        db.rollback()
        return _reserve_failed_response(items, skus_by_id)

    out_of_stock_events: list[tuple[dict[str, Any], Any]] = []
    for item in items:
        sku = skus_by_id[item["sku_id"]]
        sku.active_quantity -= item["quantity"]
        sku.reserved_quantity += item["quantity"]
        if sku.active_quantity == 0:
            event_payload = build_sku_out_of_stock_event(sku.id, sku.product_id)
            outbox_event = record_b2c_outbox_event(db, event_payload)
            out_of_stock_events.append((event_payload, outbox_event))

    result = {
        "order_id": order_id,
        "status": "RESERVED",
        "reserved_at": _utc_now(),
    }
    db.add(
        ReserveOperation(
            idempotency_key=idempotency_key,
            result_json=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        )
    )
    db.commit()

    for event_payload, outbox_event in out_of_stock_events:
        dispatch_b2c_and_mark_sent(db, b2c_dispatcher, event_payload, outbox_event)

    return result


@router.post("/inventory/unreserve")
def unreserve_skus(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
) -> dict[str, str]:
    _require_service_key(x_service_key)
    order_id = _require_uuid(payload, "order_id")
    existing_operation = db.get(UnreserveOperation, order_id)
    if existing_operation is not None:
        return json.loads(existing_operation.result_json)

    items = _validate_items(payload)
    skus_by_id = _load_skus_for_update(db, [item["sku_id"] for item in items])
    for item in items:
        sku = skus_by_id.get(item["sku_id"])
        if sku is None:
            raise api_error(404, "NOT_FOUND", "SKU not found")
        if sku.reserved_quantity < item["quantity"]:
            raise api_error(409, "CONFLICT", "Cannot unreserve more than reserved quantity")

    for item in items:
        sku = skus_by_id[item["sku_id"]]
        sku.active_quantity += item["quantity"]
        sku.reserved_quantity -= item["quantity"]

    result = {
        "order_id": order_id,
        "status": "UNRESERVED",
        "processed_at": _utc_now(),
    }
    db.add(
        UnreserveOperation(
            order_id=order_id,
            result_json=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        )
    )
    db.commit()
    return result


@router.post("/inventory/fulfill")
def fulfill_skus(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    x_service_key: str | None = Header(default=None, alias="X-Service-Key"),
) -> dict[str, str]:
    _require_service_key(x_service_key)
    order_id = _require_uuid(payload, "order_id")
    existing_operation = db.get(FulfillOperation, order_id)
    if existing_operation is not None:
        return json.loads(existing_operation.result_json)

    items = _validate_items(payload)
    skus_by_id = _load_skus_for_update(db, [item["sku_id"] for item in items])
    for item in items:
        sku = skus_by_id.get(item["sku_id"])
        if sku is None:
            raise api_error(404, "NOT_FOUND", "SKU not found")
        if sku.reserved_quantity < item["quantity"]:
            raise api_error(409, "CONFLICT", "Cannot fulfill more than reserved quantity")

    for item in items:
        sku = skus_by_id[item["sku_id"]]
        sku.reserved_quantity -= item["quantity"]

    result = {
        "order_id": order_id,
        "status": "FULFILLED",
        "processed_at": _utc_now(),
    }
    db.add(
        FulfillOperation(
            order_id=order_id,
            result_json=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        )
    )
    db.commit()
    return result
