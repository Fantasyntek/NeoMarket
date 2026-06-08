from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from typing import Any, Protocol
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.models import B2BOutboxEvent


B2B_URL = os.getenv("B2B_URL", "http://b2b:8000")
B2B_SERVICE_KEY = os.getenv("B2B_SERVICE_KEY", "dev-service-key")


class B2BDispatcher(Protocol):
    def send_moderation_event(self, payload: dict[str, Any]) -> None:
        pass


class HttpB2BDispatcher:
    def __init__(
        self,
        base_url: str = B2B_URL,
        service_key: str = B2B_SERVICE_KEY,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key

    def send_moderation_event(self, payload: dict[str, Any]) -> None:
        response = httpx.post(
            f"{self.base_url}/api/v1/moderation/events",
            json=payload,
            headers={"X-Service-Key": self.service_key},
            timeout=5.0,
        )
        response.raise_for_status()


def get_b2b_dispatcher() -> B2BDispatcher:
    return HttpB2BDispatcher()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_moderated_event(
    product_id: str,
    moderator_id: str,
    occurred_at: datetime,
) -> dict[str, Any]:
    return {
        "idempotency_key": str(uuid4()),
        "product_id": product_id,
        "event_type": "MODERATED",
        "moderator_id": moderator_id,
        "occurred_at": utc_now_iso(occurred_at),
    }


def record_b2b_outbox_event(
    db: Session,
    payload: dict[str, Any],
) -> B2BOutboxEvent:
    outbox_event = B2BOutboxEvent(
        idempotency_key=payload["idempotency_key"],
        product_id=payload["product_id"],
        event_type=payload["event_type"],
        payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        status="PENDING",
    )
    db.add(outbox_event)
    return outbox_event


def dispatch_b2b_and_mark_sent(
    db: Session,
    dispatcher: B2BDispatcher,
    payload: dict[str, Any],
    outbox_event: B2BOutboxEvent,
) -> None:
    outbox_event.attempts += 1
    try:
        dispatcher.send_moderation_event(payload)
    except Exception:
        db.commit()
        return

    outbox_event.status = "SENT"
    outbox_event.sent_at = utc_now()
    db.commit()
