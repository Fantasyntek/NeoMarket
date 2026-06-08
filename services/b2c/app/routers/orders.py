from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import CurrentUser, require_user
from app.b2b_client import (
    B2BClient,
    B2BResponseError,
    B2BUnavailableError,
    get_b2b_client,
)
from app.cancellation_retry import (
    cancellation_items,
    schedule_cancellation_retry,
)
from app.database import get_db
from app.errors import api_error
from app.models import Order, OrderItem


router = APIRouter(prefix="/api/v1/orders", tags=["Orders"])

ORDER_STATUSES = {
    "CREATED",
    "PAID",
    "ASSEMBLING",
    "DELIVERING",
    "DELIVERED",
    "CANCELLED",
    "CANCEL_PENDING",
}


def _normalize_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise api_error(400, "INVALID_REQUEST", f"{field_name} is required")
    try:
        return str(UUID(value))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", f"{field_name} must be a valid UUID")


def _idempotency_key(payload: dict[str, Any], header_value: str | None) -> str:
    body_value = payload.get("idempotency_key")
    if body_value is not None and header_value is not None:
        normalized_body = _normalize_uuid(body_value, "idempotency_key")
        normalized_header = _normalize_uuid(header_value, "Idempotency-Key")
        if normalized_body != normalized_header:
            raise api_error(
                400,
                "INVALID_REQUEST",
                "Body and header idempotency keys must match",
            )
        return normalized_body
    return _normalize_uuid(
        body_value if body_value is not None else header_value,
        "idempotency_key",
    )


def _checkout_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = payload.get("items")
    if raw_items is None:
        raw_items = payload.get("items_snapshot")
    if not isinstance(raw_items, list) or not raw_items:
        raise api_error(400, "INVALID_REQUEST", "items must be a non-empty array")

    quantities_by_sku: dict[str, int] = {}
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise api_error(
                400,
                "INVALID_REQUEST",
                f"items[{index}] must be an object",
            )
        sku_id = _normalize_uuid(raw_item.get("sku_id"), f"items[{index}].sku_id")
        quantity = raw_item.get("quantity")
        if (
            not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or quantity < 1
        ):
            raise api_error(
                422,
                "INVALID_QUANTITY",
                "quantity must be at least 1 for every item",
            )
        quantities_by_sku[sku_id] = quantities_by_sku.get(sku_id, 0) + quantity
    return [
        {"sku_id": sku_id, "quantity": quantities_by_sku[sku_id]}
        for sku_id in sorted(quantities_by_sku)
    ]


def _request_hash(payload: dict[str, Any], items: list[dict[str, Any]]) -> str:
    stable_payload = {
        "items": items,
        "delivery_address": payload.get("delivery_address"),
        "address_id": payload.get("address_id"),
        "payment_method_id": payload.get("payment_method_id"),
        "comment": payload.get("comment"),
    }
    encoded = json.dumps(
        stable_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _effective_price(sku: dict[str, Any]) -> int:
    return max(int(sku.get("price", 0)) - int(sku.get("discount", 0)), 0)


def _active_quantity(sku: dict[str, Any]) -> int:
    return int(
        sku.get(
            "active_quantity",
            sku.get("available_quantity", sku.get("stock_quantity", 0)),
        )
    )


def _image_url(sku: dict[str, Any]) -> str | None:
    images = sku.get("images") or []
    if images:
        return images[0].get("url")
    image = sku.get("image")
    return str(image) if image else None


def _order_items(db: Session, order_id: str) -> list[OrderItem]:
    return (
        db.query(OrderItem)
        .filter(OrderItem.order_id == order_id)
        .order_by(OrderItem.id)
        .all()
    )


def _serialize_order(db: Session, order: Order) -> dict[str, Any]:
    items = [
        {
            "id": item.id,
            "sku_id": item.sku_id,
            "product_id": item.product_id,
            "product_title": item.product_title,
            "sku_name": item.sku_name,
            "name": f"{item.product_title} {item.sku_name}".strip(),
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "line_total": item.line_total,
            "image_url": item.image_url,
        }
        for item in _order_items(db, order.id)
    ]
    created_at = order.created_at.isoformat()
    updated_at = order.updated_at.isoformat()
    return {
        "id": order.id,
        "buyer_id": order.user_id,
        "status": order.status,
        "items": items,
        "total_amount": order.total_amount,
        "subtotal": order.total_amount,
        "delivery_cost": 0,
        "total": order.total_amount,
        "delivery_address": order.delivery_address,
        "created_at": created_at,
        "updated_at": updated_at,
        "paid_at": updated_at if order.status == "PAID" else None,
    }


def _order_not_found() -> None:
    raise api_error(404, "ORDER_NOT_FOUND", "Order not found")


def _owned_order(
    db: Session,
    *,
    order_id: str,
    user_id: str,
) -> Order:
    try:
        normalized_order_id = str(UUID(order_id))
    except ValueError:
        _order_not_found()
    order = (
        db.query(Order)
        .filter(
            Order.id == normalized_order_id,
            Order.user_id == user_id,
        )
        .one_or_none()
    )
    if order is None:
        _order_not_found()
    return order


@router.get("")
def list_orders(
    current_user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    if status is not None and status not in ORDER_STATUSES:
        raise api_error(
            400,
            "INVALID_STATUS",
            f"status must be one of: {', '.join(sorted(ORDER_STATUSES))}",
        )

    items_count = (
        db.query(func.count(OrderItem.id))
        .filter(OrderItem.order_id == Order.id)
        .correlate(Order)
        .scalar_subquery()
    )
    query = db.query(Order, items_count.label("items_count")).filter(
        Order.user_id == current_user.user_id
    )
    if status is not None:
        query = query.filter(Order.status == status)

    total_count = query.count()
    rows = (
        query.order_by(Order.created_at.desc(), Order.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "items": [
            {
                "id": order.id,
                "status": order.status,
                "total_amount": order.total_amount,
                "items_count": int(count),
                "created_at": order.created_at.isoformat(),
                "updated_at": order.updated_at.isoformat(),
            }
            for order, count in rows
        ],
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{order_id}")
def get_order(
    order_id: str,
    current_user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    order = _owned_order(
        db,
        order_id=order_id,
        user_id=current_user.user_id,
    )
    return _serialize_order(db, order)


@router.post("/{order_id}/cancel", response_model=None)
def cancel_order(
    order_id: str,
    current_user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
) -> dict[str, Any] | JSONResponse:
    order = _owned_order(
        db,
        order_id=order_id,
        user_id=current_user.user_id,
    )
    if order.status not in {"CREATED", "PAID"}:
        return JSONResponse(
            status_code=409,
            content={
                "code": "CANCEL_NOT_ALLOWED",
                "message": f"Cancellation is not allowed for status {order.status}",
                "current_status": order.status,
            },
        )

    items = cancellation_items(db, order.id)
    try:
        b2b_client.unreserve(order_id=order.id, items=items)
    except B2BUnavailableError as exc:
        schedule_cancellation_retry(db, order, exc)
        db.refresh(order)
        return _serialize_order(db, order)
    except B2BResponseError as exc:
        if exc.status_code >= 500:
            schedule_cancellation_retry(db, order, exc)
            db.refresh(order)
            return _serialize_order(db, order)
        raise api_error(
            exc.status_code,
            str(exc.payload.get("code", "B2B_ERROR")),
            str(exc.payload.get("message", "B2B request failed")),
        )

    order.status = "CANCELLED"
    db.commit()
    db.refresh(order)
    return _serialize_order(db, order)


def _existing_order_response(
    db: Session,
    order: Order,
    current_user: CurrentUser,
    request_hash: str,
) -> JSONResponse | None:
    if order.user_id != current_user.user_id or order.request_hash != request_hash:
        raise api_error(
            409,
            "IDEMPOTENCY_CONFLICT",
            "idempotency_key was already used for another checkout",
        )
    if order.status == "PAID":
        return JSONResponse(status_code=200, content=_serialize_order(db, order))
    return None


def _claim_idempotency_key(
    db: Session,
    *,
    current_user: CurrentUser,
    idempotency_key: str,
    request_hash: str,
    delivery_address: str | None,
) -> tuple[Order, JSONResponse | None]:
    existing = (
        db.query(Order)
        .filter(Order.idempotency_key == idempotency_key)
        .one_or_none()
    )
    if existing is not None:
        return existing, _existing_order_response(
            db,
            existing,
            current_user,
            request_hash,
        )

    order = Order(
        user_id=current_user.user_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status="CREATED",
        delivery_address=delivery_address,
        total_amount=0,
    )
    db.add(order)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(Order)
            .filter(Order.idempotency_key == idempotency_key)
            .one()
        )
        return existing, _existing_order_response(
            db,
            existing,
            current_user,
            request_hash,
        )
    db.refresh(order)
    return order, None


def _batch_skus(
    b2b_client: B2BClient,
    sku_ids: list[str],
) -> list[dict[str, Any]]:
    lookups: list[dict[str, Any]] = []
    try:
        for start in range(0, len(sku_ids), 100):
            lookups.extend(b2b_client.batch_skus(sku_ids[start : start + 100]))
    except B2BUnavailableError:
        raise api_error(
            503,
            "B2B_UNAVAILABLE",
            "Product service is temporarily unavailable",
        )
    except B2BResponseError as exc:
        if exc.status_code >= 500:
            raise api_error(
                503,
                "B2B_UNAVAILABLE",
                "Product service is temporarily unavailable",
            )
        raise api_error(
            exc.status_code,
            str(exc.payload.get("code", "B2B_ERROR")),
            str(exc.payload.get("message", "B2B request failed")),
        )
    return lookups


def _failed_items(
    requested_items: list[dict[str, Any]],
    lookups_by_sku: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    for requested in requested_items:
        lookup = lookups_by_sku.get(requested["sku_id"])
        if lookup is None:
            failed.append(
                {
                    "sku_id": requested["sku_id"],
                    "requested": requested["quantity"],
                    "available": 0,
                    "reason": "SKU_NOT_FOUND",
                }
            )
            continue

        product = lookup["product"]
        sku = lookup["sku"]
        available = _active_quantity(sku)
        reason = None
        if product.get("deleted") is True:
            reason = "PRODUCT_DELETED"
        elif product.get("status") != "MODERATED":
            reason = "PRODUCT_BLOCKED"
        elif available == 0:
            reason = "OUT_OF_STOCK"
        elif available < requested["quantity"]:
            reason = "INSUFFICIENT_STOCK"
        if reason is not None:
            failed.append(
                {
                    "sku_id": requested["sku_id"],
                    "requested": requested["quantity"],
                    "available": available,
                    "reason": reason,
                }
            )
    return failed


def _reserve_failed(failed_items: list[dict[str, Any]]) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "code": "RESERVE_FAILED",
            "message": "Failed to reserve order items",
            "failed_items": failed_items,
        },
    )


def _delete_pending_order(db: Session, order: Order) -> None:
    db.query(OrderItem).filter(OrderItem.order_id == order.id).delete(
        synchronize_session=False
    )
    db.delete(order)
    db.commit()


def _store_snapshots(
    db: Session,
    order: Order,
    requested_items: list[dict[str, Any]],
    lookups_by_sku: dict[str, dict[str, Any]],
) -> None:
    if _order_items(db, order.id):
        return
    total_amount = 0
    for requested in requested_items:
        lookup = lookups_by_sku[requested["sku_id"]]
        product = lookup["product"]
        sku = lookup["sku"]
        unit_price = _effective_price(sku)
        line_total = unit_price * requested["quantity"]
        total_amount += line_total
        db.add(
            OrderItem(
                order_id=order.id,
                sku_id=requested["sku_id"],
                product_id=str(product["id"]),
                product_title=str(product.get("title", "")),
                sku_name=str(sku.get("name", "")),
                quantity=requested["quantity"],
                unit_price=unit_price,
                line_total=line_total,
                image_url=_image_url(sku),
            )
        )
    order.total_amount = total_amount
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if len(_order_items(db, order.id)) != len(requested_items):
            raise


def _reserve_inventory(
    b2b_client: B2BClient,
    *,
    idempotency_key: str,
    order_id: str,
    items: list[dict[str, Any]],
) -> JSONResponse | None:
    try:
        b2b_client.reserve(
            idempotency_key=idempotency_key,
            order_id=order_id,
            items=items,
        )
    except B2BUnavailableError:
        raise api_error(
            503,
            "B2B_UNAVAILABLE",
            "Product service is temporarily unavailable",
        )
    except B2BResponseError as exc:
        if exc.status_code >= 500:
            raise api_error(
                503,
                "B2B_UNAVAILABLE",
                "Product service is temporarily unavailable",
            )
        if exc.status_code == 409:
            return _reserve_failed(list(exc.payload.get("failed_items", [])))
        raise api_error(
            exc.status_code,
            str(exc.payload.get("code", "B2B_ERROR")),
            str(exc.payload.get("message", "B2B request failed")),
        )
    return None


@router.post("", response_model=None)
def create_order(
    payload: dict[str, Any],
    current_user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
    idempotency_header: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> JSONResponse:
    key = _idempotency_key(payload, idempotency_header)
    items = _checkout_items(payload)
    request_hash = _request_hash(payload, items)
    delivery_address = payload.get("delivery_address")
    if delivery_address is not None and not isinstance(delivery_address, str):
        raise api_error(
            400,
            "INVALID_REQUEST",
            "delivery_address must be a string",
        )

    order, existing_response = _claim_idempotency_key(
        db,
        current_user=current_user,
        idempotency_key=key,
        request_hash=request_hash,
        delivery_address=delivery_address,
    )
    if existing_response is not None:
        return existing_response

    try:
        lookups = _batch_skus(
            b2b_client,
            [item["sku_id"] for item in items],
        )
        lookups_by_sku = {
            str(lookup["sku"]["id"]): lookup
            for lookup in lookups
        }
        failed_items = _failed_items(items, lookups_by_sku)
        if failed_items:
            _delete_pending_order(db, order)
            return _reserve_failed(failed_items)

        _store_snapshots(db, order, items, lookups_by_sku)
        reserve_error = _reserve_inventory(
            b2b_client,
            idempotency_key=key,
            order_id=order.id,
            items=items,
        )
        if reserve_error is not None:
            _delete_pending_order(db, order)
            return reserve_error
    except Exception:
        if db.get(Order, order.id) is not None and order.status != "PAID":
            _delete_pending_order(db, order)
        raise

    order.status = "PAID"
    db.commit()
    db.refresh(order)
    return JSONResponse(status_code=201, content=_serialize_order(db, order))
