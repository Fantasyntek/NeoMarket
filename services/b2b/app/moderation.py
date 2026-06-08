from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.models import ModerationOutboxEvent, Product


MODERATION_URL = os.getenv("MODERATION_URL", "http://moderation:8000")
MODERATION_SERVICE_KEY = os.getenv("MODERATION_SERVICE_KEY", "dev-service-key")
EVENT_TYPE_BY_ACTION = {
    "CREATED": "PRODUCT_CREATED",
    "EDITED": "PRODUCT_EDITED",
    "DELETED": "PRODUCT_DELETED",
}


class ModerationDispatcher(Protocol):
    def send_product_event(self, payload: dict[str, Any]) -> None:
        pass


class HttpModerationDispatcher:
    def __init__(
        self,
        base_url: str = MODERATION_URL,
        service_key: str = MODERATION_SERVICE_KEY,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key

    def send_product_event(self, payload: dict[str, Any]) -> None:
        response = httpx.post(
            f"{self.base_url}/api/v1/b2b/events",
            json=payload,
            headers={"X-Service-Key": self.service_key},
            timeout=5.0,
        )
        response.raise_for_status()


def get_moderation_dispatcher() -> ModerationDispatcher:
    return HttpModerationDispatcher()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _product_snapshot(product: Product) -> dict[str, Any]:
    return {
        "id": product.id,
        "seller_id": product.seller_id,
        "category_id": product.category_id,
        "title": product.title,
        "slug": product.slug,
        "description": product.description,
        "status": product.status,
        "deleted": product.deleted,
        "images": [
            {"id": image.id, "url": image.url, "ordering": image.ordering}
            for image in product.images
        ],
        "characteristics": [
            {"name": item.name, "value": item.value}
            for item in product.characteristics
        ],
        "skus": [
            {
                "id": sku.id,
                "name": sku.name,
                "price": sku.price,
                "cost_price": sku.cost_price,
                "discount": sku.discount,
                "image": sku.image,
                "active_quantity": sku.active_quantity,
                "reserved_quantity": sku.reserved_quantity,
                "characteristics": [
                    {"name": item.name, "value": item.value}
                    for item in sku.characteristics
                ],
            }
            for sku in product.skus
        ],
    }


def build_product_event(
    product: Product,
    event: str,
    *,
    json_before: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        event_type = EVENT_TYPE_BY_ACTION[event]
    except KeyError as exc:
        raise ValueError(f"Unsupported moderation event action: {event}") from exc

    event_payload: dict[str, Any] = {"product_id": product.id}
    if event_type != "PRODUCT_DELETED":
        event_payload.update(
            {
                "seller_id": product.seller_id,
                "category_id": product.category_id,
                "json_after": _product_snapshot(product),
            }
        )
    if event_type == "PRODUCT_EDITED":
        event_payload["json_before"] = json_before or {}

    return {
        "idempotency_key": str(uuid4()),
        "event_type": event_type,
        "occurred_at": utc_now_iso(),
        "payload": event_payload,
    }


def record_outbox_event(
    db: Session,
    payload: dict[str, Any],
    *,
    seller_id: str | None = None,
) -> ModerationOutboxEvent:
    event_payload = payload["payload"]
    outbox_event = ModerationOutboxEvent(
        idempotency_key=payload["idempotency_key"],
        event=payload["event_type"],
        product_id=event_payload["product_id"],
        seller_id=seller_id or event_payload["seller_id"],
        payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        status="PENDING",
    )
    db.add(outbox_event)
    return outbox_event


def dispatch_and_mark_sent(
    db: Session,
    dispatcher: ModerationDispatcher,
    payload: dict[str, Any] | None,
    outbox_event: ModerationOutboxEvent | None,
) -> None:
    if payload is None or outbox_event is None:
        return

    try:
        dispatcher.send_product_event(payload)
    except Exception:
        return

    outbox_event.status = "SENT"
    outbox_event.sent_at = datetime.now(timezone.utc)
    db.commit()
