from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import decode_access_token
from app.b2b_client import (
    B2BClient,
    B2BResponseError,
    B2BUnavailableError,
    get_b2b_client,
)
from app.database import get_db
from app.errors import api_error
from app.models import CartItem


router = APIRouter(prefix="/api/v1/cart", tags=["Cart"])


@dataclass(frozen=True)
class CartIdentity:
    user_id: str | None
    session_id: str | None

    @property
    def response_id(self) -> str:
        return self.user_id or self.session_id or ""


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", f"{field_name} must be a valid UUID")


def _resolve_identity(
    authorization: str | None,
    session_id: str | None,
) -> CartIdentity:
    normalized_session_id = (
        _normalize_uuid(session_id, "X-Session-Id") if session_id else None
    )
    if authorization:
        if not authorization.startswith("Bearer "):
            raise api_error(401, "UNAUTHORIZED", "Invalid authorization header")
        payload = decode_access_token(authorization.removeprefix("Bearer ").strip())
        user_id = payload.get("sub")
        if not isinstance(user_id, str):
            raise api_error(401, "UNAUTHORIZED", "sub claim is required")
        return CartIdentity(
            user_id=_normalize_uuid(user_id, "sub claim"),
            session_id=normalized_session_id,
        )
    if normalized_session_id:
        return CartIdentity(user_id=None, session_id=normalized_session_id)
    raise api_error(
        400,
        "MISSING_CART_IDENTITY",
        "Bearer JWT or X-Session-Id is required",
    )


def _identity_filter(query: Any, identity: CartIdentity) -> Any:
    if identity.user_id:
        return query.filter(CartItem.user_id == identity.user_id)
    return query.filter(CartItem.session_id == identity.session_id)


def _cart_items(db: Session, identity: CartIdentity) -> list[CartItem]:
    return (
        _identity_filter(db.query(CartItem), identity)
        .order_by(CartItem.created_at, CartItem.id)
        .all()
    )


def _merge_guest_cart(db: Session, identity: CartIdentity) -> None:
    if not identity.user_id or not identity.session_id:
        return
    guest_items = (
        db.query(CartItem)
        .filter(CartItem.session_id == identity.session_id)
        .all()
    )
    if not guest_items:
        return
    user_items = {
        item.sku_id: item
        for item in db.query(CartItem)
        .filter(CartItem.user_id == identity.user_id)
        .all()
    }
    for guest_item in guest_items:
        user_item = user_items.get(guest_item.sku_id)
        if user_item is not None:
            user_item.quantity = max(user_item.quantity, guest_item.quantity)
            db.delete(guest_item)
        else:
            guest_item.user_id = identity.user_id
            guest_item.session_id = None
    db.commit()


def _b2b_error(exc: B2BResponseError, not_found_message: str) -> None:
    if exc.status_code == 404:
        raise api_error(404, "SKU_NOT_FOUND", not_found_message)
    raise api_error(
        exc.status_code,
        str(exc.payload.get("code", "B2B_ERROR")),
        str(exc.payload.get("message", "B2B request failed")),
    )


def _get_sku(b2b_client: B2BClient, sku_id: str) -> dict[str, Any]:
    try:
        return b2b_client.get_sku(sku_id)
    except B2BUnavailableError:
        raise api_error(503, "B2B_UNAVAILABLE", "Inventory is temporarily unavailable")
    except B2BResponseError as exc:
        _b2b_error(exc, "SKU not found or product is unavailable")


def _batch_skus(
    b2b_client: B2BClient,
    sku_ids: list[str],
) -> list[dict[str, Any]]:
    lookups: list[dict[str, Any]] = []
    try:
        for start in range(0, len(sku_ids), 100):
            lookups.extend(b2b_client.batch_skus(sku_ids[start : start + 100]))
    except B2BUnavailableError:
        raise api_error(503, "B2B_UNAVAILABLE", "Inventory is temporarily unavailable")
    except B2BResponseError as exc:
        _b2b_error(exc, "SKU not found")
    return lookups


def _batch_products(
    b2b_client: B2BClient,
    product_ids: list[str],
) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    try:
        for start in range(0, len(product_ids), 100):
            products.extend(
                b2b_client.batch_products(product_ids[start : start + 100])
            )
    except B2BUnavailableError:
        raise api_error(503, "B2B_UNAVAILABLE", "Catalog is temporarily unavailable")
    except B2BResponseError as exc:
        _b2b_error(exc, "Product not found")
    return products


def _batch_cart_lookups(
    b2b_client: B2BClient,
    stored_items: list[CartItem],
) -> dict[str, dict[str, Any]]:
    product_ids = list(dict.fromkeys(item.product_id for item in stored_items))
    products = _batch_products(b2b_client, product_ids)
    lookups_by_sku = {
        str(sku["id"]): {"product": product, "sku": sku}
        for product in products
        for sku in product.get("skus", [])
    }

    unresolved_sku_ids = [
        item.sku_id for item in stored_items if item.sku_id not in lookups_by_sku
    ]
    if unresolved_sku_ids:
        fallback_lookups = _batch_skus(b2b_client, unresolved_sku_ids)
        lookups_by_sku.update(
            {
                str(lookup["sku"]["id"]): lookup
                for lookup in fallback_lookups
            }
        )
    return lookups_by_sku


def _active_quantity(sku: dict[str, Any]) -> int:
    return int(
        sku.get(
            "active_quantity",
            sku.get("available_quantity", sku.get("stock_quantity", 0)),
        )
    )


def _effective_price(sku: dict[str, Any]) -> int:
    return max(int(sku.get("price", 0)) - int(sku.get("discount", 0)), 0)


def _image_payload(sku: dict[str, Any]) -> dict[str, Any] | None:
    images = sku.get("images") or []
    if images:
        image = images[0]
        return {
            "id": image.get("id", sku.get("id")),
            "url": image.get("url"),
            "ordering": int(image.get("ordering", 0)),
        }
    if sku.get("image"):
        return {"id": sku.get("id"), "url": sku["image"], "ordering": 0}
    return None


def _unavailable_reason(
    product: dict[str, Any],
    available_quantity: int,
) -> str | None:
    if product.get("deleted") is True:
        return "PRODUCT_DELETED"
    status = product.get("status")
    if status in {"BLOCKED", "HARD_BLOCKED"}:
        return "PRODUCT_BLOCKED"
    if status == "ON_MODERATION":
        return "ON_MODERATION"
    if available_quantity <= 0:
        return "OUT_OF_STOCK"
    return None


def _serialize_cart(
    db: Session,
    identity: CartIdentity,
    b2b_client: B2BClient,
) -> dict[str, Any]:
    stored_items = _cart_items(db, identity)
    if not stored_items:
        return {
            "id": identity.response_id,
            "items": [],
            "items_count": 0,
            "subtotal": 0,
            "is_valid": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_amount": 0,
                "total_items": 0,
                "unavailable_count": 0,
                "checkout_ready": False,
            },
            "checkout_payload": {"items": []},
        }

    lookup_by_sku = _batch_cart_lookups(b2b_client, stored_items)
    response_items: list[dict[str, Any]] = []
    checkout_items: list[dict[str, Any]] = []
    subtotal = 0
    unavailable_count = 0

    for stored_item in stored_items:
        lookup = lookup_by_sku.get(stored_item.sku_id)
        if lookup is None:
            unavailable_reason = stored_item.unavailable_reason or "PRODUCT_DELETED"
            response_items.append(
                {
                    "id": stored_item.id,
                    "sku_id": stored_item.sku_id,
                    "product_id": stored_item.product_id,
                    "name": "Unavailable product",
                    "quantity": stored_item.quantity,
                    "unit_price": 0,
                    "line_total": 0,
                    "available_quantity": 0,
                    "is_available": False,
                    "available": False,
                    "unavailable_reason": unavailable_reason,
                    "image": None,
                }
            )
            unavailable_count += 1
            continue

        product = lookup["product"]
        sku = lookup["sku"]
        available_quantity = _active_quantity(sku)
        unavailable_reason = stored_item.unavailable_reason or _unavailable_reason(
            product,
            available_quantity,
        )
        is_available = unavailable_reason is None
        unit_price = _effective_price(sku)
        line_total = unit_price * stored_item.quantity if is_available else 0
        subtotal += line_total
        if not is_available:
            unavailable_count += 1
        response_items.append(
            {
                "id": stored_item.id,
                "sku_id": stored_item.sku_id,
                "product_id": stored_item.product_id,
                "name": f"{product.get('title', '')} {sku.get('name', '')}".strip(),
                "sku_code": sku.get("article"),
                "quantity": stored_item.quantity,
                "unit_price": unit_price,
                "line_total": line_total,
                "available_quantity": available_quantity,
                "is_available": is_available,
                "available": is_available,
                "unavailable_reason": unavailable_reason,
                "image": _image_payload(sku),
            }
        )
        if is_available:
            checkout_items.append(
                {
                    "sku_id": stored_item.sku_id,
                    "quantity": stored_item.quantity,
                    "unit_price": unit_price,
                }
            )

    total_items = sum(item.quantity for item in stored_items)
    checkout_ready = bool(response_items) and unavailable_count == 0 and all(
        item["quantity"] <= item["available_quantity"] for item in response_items
    )
    updated_at = max(item.updated_at for item in stored_items).isoformat()
    return {
        "id": identity.response_id,
        "items": response_items,
        "items_count": total_items,
        "subtotal": subtotal,
        "is_valid": checkout_ready,
        "updated_at": updated_at,
        "summary": {
            "total_amount": subtotal,
            "total_items": total_items,
            "unavailable_count": unavailable_count,
            "checkout_ready": checkout_ready,
        },
        "checkout_payload": {"items": checkout_items},
    }


def _prepare_identity(
    db: Session,
    authorization: str | None,
    session_id: str | None,
) -> CartIdentity:
    identity = _resolve_identity(authorization, session_id)
    _merge_guest_cart(db, identity)
    if identity.user_id:
        return CartIdentity(user_id=identity.user_id, session_id=None)
    return identity


def _validate_quantity(payload: dict[str, Any]) -> int:
    quantity = payload.get("quantity")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        raise api_error(400, "INVALID_QUANTITY", "quantity must be at least 1")
    return quantity


@router.get("")
def get_cart(
    authorization: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
) -> dict[str, Any]:
    identity = _prepare_identity(db, authorization, x_session_id)
    return _serialize_cart(db, identity, b2b_client)


@router.post("/items")
def add_cart_item(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
) -> JSONResponse:
    identity = _prepare_identity(db, authorization, x_session_id)
    raw_sku_id = payload.get("sku_id")
    if not isinstance(raw_sku_id, str):
        raise api_error(400, "INVALID_REQUEST", "sku_id is required")
    sku_id = _normalize_uuid(raw_sku_id, "sku_id")
    quantity = _validate_quantity(payload)
    lookup = _get_sku(b2b_client, sku_id)
    available_quantity = _active_quantity(lookup["sku"])

    existing = _identity_filter(
        db.query(CartItem).filter(CartItem.sku_id == sku_id),
        identity,
    ).one_or_none()
    resulting_quantity = quantity + (existing.quantity if existing else 0)
    if available_quantity < resulting_quantity:
        raise api_error(409, "INSUFFICIENT_STOCK", "Insufficient SKU stock")

    if existing is not None:
        existing.quantity = resulting_quantity
        status_code = 200
    else:
        db.add(
            CartItem(
                user_id=identity.user_id,
                session_id=identity.session_id,
                product_id=str(lookup["product"]["id"]),
                sku_id=sku_id,
                quantity=quantity,
            )
        )
        status_code = 201
    db.commit()
    return JSONResponse(
        status_code=status_code,
        content=_serialize_cart(db, identity, b2b_client),
    )


def _find_owned_item(
    db: Session,
    identity: CartIdentity,
    item_id: str,
) -> CartItem:
    normalized_id = _normalize_uuid(item_id, "item_id")
    item = _identity_filter(
        db.query(CartItem).filter(CartItem.id == normalized_id),
        identity,
    ).one_or_none()
    if item is None:
        raise api_error(404, "NOT_FOUND", "Cart item not found")
    return item


@router.put("/items/{item_id}")
def update_cart_item(
    item_id: str,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
) -> dict[str, Any]:
    identity = _prepare_identity(db, authorization, x_session_id)
    item = _find_owned_item(db, identity, item_id)
    quantity = _validate_quantity(payload)
    lookup = _get_sku(b2b_client, item.sku_id)
    if _active_quantity(lookup["sku"]) < quantity:
        raise api_error(409, "INSUFFICIENT_STOCK", "Insufficient SKU stock")
    item.quantity = quantity
    db.commit()
    return _serialize_cart(db, identity, b2b_client)


@router.patch("/items/{sku_id}")
def patch_cart_item_by_sku(
    sku_id: str,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
) -> dict[str, Any]:
    identity = _prepare_identity(db, authorization, x_session_id)
    normalized_sku_id = _normalize_uuid(sku_id, "sku_id")
    item = _identity_filter(
        db.query(CartItem).filter(CartItem.sku_id == normalized_sku_id),
        identity,
    ).one_or_none()
    if item is None:
        raise api_error(404, "NOT_FOUND", "Cart item not found")
    return update_cart_item(
        item.id,
        payload,
        authorization,
        identity.session_id,
        db,
        b2b_client,
    )


@router.delete("/items/{item_id}")
def delete_cart_item(
    item_id: str,
    authorization: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
) -> dict[str, Any]:
    identity = _prepare_identity(db, authorization, x_session_id)
    normalized_id = _normalize_uuid(item_id, "item_id")
    item = _identity_filter(
        db.query(CartItem).filter(
            (CartItem.id == normalized_id) | (CartItem.sku_id == normalized_id)
        ),
        identity,
    ).one_or_none()
    if item is None:
        raise api_error(404, "NOT_FOUND", "Cart item not found")
    db.delete(item)
    db.commit()
    return _serialize_cart(db, identity, b2b_client)


@router.delete("", status_code=204)
def clear_cart(
    authorization: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    db: Session = Depends(get_db),
) -> Response:
    identity = _prepare_identity(db, authorization, x_session_id)
    _identity_filter(db.query(CartItem), identity).delete(synchronize_session=False)
    db.commit()
    return Response(status_code=204)


@router.post("/merge")
def merge_cart(
    authorization: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    db: Session = Depends(get_db),
    b2b_client: B2BClient = Depends(get_b2b_client),
) -> dict[str, Any]:
    if not authorization:
        raise api_error(401, "UNAUTHORIZED", "Authorization required")
    if not x_session_id:
        raise api_error(400, "MISSING_CART_IDENTITY", "X-Session-Id is required")
    identity = _prepare_identity(db, authorization, x_session_id)
    return _serialize_cart(db, identity, b2b_client)
