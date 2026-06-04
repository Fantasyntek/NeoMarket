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
            f"{self.base_url}/api/v1/events/product",
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


def build_product_event(product: Product, event: str) -> dict[str, Any]:
    return {
        "idempotency_key": str(uuid4()),
        "product_id": product.id,
        "seller_id": product.seller_id,
        "event": event,
        "date": utc_now_iso(),
    }


def record_outbox_event(db: Session, payload: dict[str, Any]) -> ModerationOutboxEvent:
    outbox_event = ModerationOutboxEvent(
        idempotency_key=payload["idempotency_key"],
        event=payload["event"],
        product_id=payload["product_id"],
        seller_id=payload["seller_id"],
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
