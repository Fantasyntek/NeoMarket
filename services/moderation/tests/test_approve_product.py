from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.b2b import HttpB2BDispatcher, get_b2b_dispatcher
from app.models import B2BOutboxEvent, ProductModeration
from app.retry_b2b_events import retry_pending_b2b_events


MODERATOR_ID = "11111111-2222-4333-8444-555555555555"
OTHER_MODERATOR_ID = "22222222-3333-4444-8555-666666666666"
SERVICE_HEADERS = {"X-Service-Key": "dev-service-key"}


class FakeB2BDispatcher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[dict[str, Any]] = []

    def send_moderation_event(self, payload: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("B2B unavailable")
        self.events.append(payload)


def auth_headers(moderator_id: str = MODERATOR_ID) -> dict[str, str]:
    token = create_access_token({"moderator_id": moderator_id})
    return {"Authorization": f"Bearer {token}"}


def override_b2b(client: TestClient, *, fail: bool = False) -> FakeB2BDispatcher:
    fake = FakeB2BDispatcher(fail=fail)
    client.app.dependency_overrides[get_b2b_dispatcher] = lambda: fake
    return fake


def create_review_card(
    db: Session,
    *,
    moderator_id: str = MODERATOR_ID,
    with_sku: bool = True,
) -> ProductModeration:
    snapshot: dict[str, Any] = {
        "title": "iPhone 15",
        "skus": (
            [
                {
                    "id": str(uuid4()),
                    "name": "256GB Black",
                    "active_quantity": 5,
                }
            ]
            if with_sku
            else []
        ),
    }
    card = ProductModeration(
        product_id=str(uuid4()),
        seller_id=str(uuid4()),
        category_id=str(uuid4()),
        kind="CREATE",
        status="IN_REVIEW",
        queue_priority=1,
        json_after=snapshot,
        content_revision=1,
        review_revision=1,
        moderator_id=moderator_id,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def test_approve_transitions_to_moderated_and_emits_event(
    client: TestClient,
    db_session: Session,
) -> None:
    card = create_review_card(db_session)
    fake = override_b2b(client)

    response = client.post(
        f"/api/v1/tickets/{card.id}/approve",
        json={"comment": "Looks good"},
        headers=auth_headers(),
    )

    db_session.refresh(card)
    outbox = db_session.query(B2BOutboxEvent).one()
    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"
    assert card.status == "APPROVED"
    assert card.decision_at is not None
    assert len(fake.events) == 1
    assert fake.events[0]["event_type"] == "MODERATED"
    assert fake.events[0]["product_id"] == card.product_id
    assert fake.events[0]["moderator_id"] == MODERATOR_ID
    assert fake.events[0]["idempotency_key"]
    assert fake.events[0]["occurred_at"]
    assert outbox.status == "SENT"
    assert outbox.attempts == 1


def test_approve_others_card_returns_403(
    client: TestClient,
    db_session: Session,
) -> None:
    card = create_review_card(db_session, moderator_id=OTHER_MODERATOR_ID)
    fake = override_b2b(client)

    response = client.post(
        f"/api/v1/tickets/{card.id}/approve",
        headers=auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "CARD_OWNED_BY_ANOTHER_MODERATOR"
    assert fake.events == []


def test_approve_after_edited_returns_409(
    client: TestClient,
    db_session: Session,
) -> None:
    card = create_review_card(db_session)
    fake = override_b2b(client)

    edited_response = client.post(
        "/api/v1/b2b/events",
        json={
            "idempotency_key": str(uuid4()),
            "event_type": "PRODUCT_EDITED",
            "occurred_at": "2026-06-09T12:00:00Z",
            "payload": {
                "product_id": card.product_id,
                "seller_id": card.seller_id,
                "category_id": card.category_id,
                "json_before": card.json_after,
                "json_after": {
                    **card.json_after,
                    "title": "Edited during review",
                },
            },
        },
        headers=SERVICE_HEADERS,
    )
    response = client.post(
        f"/api/v1/tickets/{card.id}/approve",
        headers=auth_headers(),
    )

    db_session.refresh(card)
    assert edited_response.status_code == 202
    assert response.status_code == 409
    assert response.json()["code"] == "PRODUCT_EDITED_DURING_REVIEW"
    assert card.status == "IN_REVIEW"
    assert fake.events == []


def test_approve_without_sku_returns_409(
    client: TestClient,
    db_session: Session,
) -> None:
    card = create_review_card(db_session, with_sku=False)
    fake = override_b2b(client)

    response = client.post(
        f"/api/v1/tickets/{card.id}/approve",
        headers=auth_headers(),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "PRODUCT_HAS_NO_SKU"
    assert fake.events == []


def test_failed_b2b_delivery_keeps_outbox_pending(
    client: TestClient,
    db_session: Session,
) -> None:
    card = create_review_card(db_session)
    override_b2b(client, fail=True)

    response = client.post(
        f"/api/v1/product-moderation/{card.id}/approve",
        headers=auth_headers(),
    )

    outbox = db_session.query(B2BOutboxEvent).one()
    assert response.status_code == 200
    assert outbox.status == "PENDING"
    assert outbox.attempts == 1

    retry_dispatcher = FakeB2BDispatcher()
    sent = retry_pending_b2b_events(db_session, retry_dispatcher)
    db_session.refresh(outbox)

    assert sent == 1
    assert outbox.status == "SENT"
    assert outbox.attempts == 2
    assert retry_dispatcher.events[0]["idempotency_key"] == outbox.idempotency_key


def test_http_dispatcher_uses_b2b_moderation_event_contract(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url: str, **kwargs: Any) -> Response:
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("app.b2b.httpx.post", fake_post)
    payload = {
        "idempotency_key": str(uuid4()),
        "product_id": str(uuid4()),
        "event_type": "MODERATED",
        "moderator_id": MODERATOR_ID,
        "occurred_at": "2026-06-09T12:00:00Z",
    }

    HttpB2BDispatcher(
        base_url="http://b2b:8000/",
        service_key="service-secret",
    ).send_moderation_event(payload)

    assert captured["url"] == "http://b2b:8000/api/v1/moderation/events"
    assert captured["json"] == payload
    assert captured["headers"] == {"X-Service-Key": "service-secret"}
