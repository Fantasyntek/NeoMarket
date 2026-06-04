from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import CurrentSeller, require_seller
from app.database import get_db
from app.errors import api_error
from app.models import Invoice, InvoiceItem, SKU


router = APIRouter(prefix="/api/v1", tags=["Invoices"])


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


def _serialize_invoice(invoice: Invoice) -> dict[str, Any]:
    return {
        "id": invoice.id,
        "seller_id": invoice.seller_id,
        "status": invoice.status,
        "created_at": invoice.created_at.isoformat(),
        "updated_at": invoice.updated_at.isoformat(),
        "accepted_at": invoice.accepted_at.isoformat() if invoice.accepted_at else None,
        "accepted_by": invoice.accepted_by,
        "items": [
            {
                "id": item.id,
                "sku_id": item.sku_id,
                "sku_name": item.sku.name,
                "quantity": item.quantity,
                "accepted_quantity": item.accepted_quantity,
            }
            for item in invoice.items
        ],
    }


@router.post("/invoices", status_code=201)
def create_invoice(
    payload: dict[str, Any],
    current_seller: CurrentSeller = Depends(require_seller),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items = _validate_items(payload)
    sku_ids = [item["sku_id"] for item in items]
    skus_by_id = {
        sku.id: sku
        for sku in db.query(SKU).filter(SKU.id.in_(sku_ids)).all()
    }

    for item in items:
        sku = skus_by_id.get(item["sku_id"])
        if sku is None:
            raise api_error(404, "NOT_FOUND", "SKU not found")
        if sku.product.seller_id != current_seller.seller_id:
            raise api_error(
                403,
                "NOT_OWNER",
                "One or more SKUs do not belong to the authenticated seller",
            )
        if sku.product.status != "MODERATED":
            raise api_error(
                400,
                "INVALID_REQUEST",
                "Invoice can only be created for MODERATED products",
            )

    invoice = Invoice(seller_id=current_seller.seller_id, status="PENDING")
    invoice.items = [
        InvoiceItem(
            sku_id=item["sku_id"],
            quantity=item["quantity"],
            accepted_quantity=None,
        )
        for item in items
    ]
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return _serialize_invoice(invoice)
