from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.models import B2COutboxEvent, Product
from app.moderation import utc_now_iso


B2C_URL = os.getenv("B2C_URL", "http://b2c:8000")
B2C_SERVICE_KEY = os.getenv("B2C_SERVICE_KEY", "dev-service-key")


class B2CDispatcher(Protocol):
    def send_product_event(self, payload: dict[str, Any]) -> None:
        pass


class HttpB2CDispatcher:
    def __init__(
        self,
        base_url: str = B2C_URL,
        service_key: str = B2C_SERVICE_KEY,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key

    def send_product_event(self, payload: dict[str, Any]) -> None:
        response = httpx.post(
            f"{self.base_url}/api/v1/events/product",
            json=payload,
            headers={"X-Service-Key": self.service_key},
            timeout=5.0,
        )
        response.raise_for_status()


def get_b2c_dispatcher() -> B2CDispatcher:
    return HttpB2CDispatcher()


def build_product_deleted_event(product: Product, sku_ids: list[str]) -> dict[str, Any]:
    return {
        "idempotency_key": str(uuid4()),
        "event": "PRODUCT_DELETED",
        "product_id": product.id,
        "sku_ids": sku_ids,
        "date": utc_now_iso(),
    }


def record_b2c_outbox_event(db: Session, payload: dict[str, Any]) -> B2COutboxEvent:
    outbox_event = B2COutboxEvent(
        idempotency_key=payload["idempotency_key"],
        event=payload["event"],
        product_id=payload["product_id"],
        payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        status="PENDING",
    )
    db.add(outbox_event)
    return outbox_event


def dispatch_b2c_and_mark_sent(
    db: Session,
    dispatcher: B2CDispatcher,
    payload: dict[str, Any] | None,
    outbox_event: B2COutboxEvent | None,
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
