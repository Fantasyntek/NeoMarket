from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.b2b import get_b2b_dispatcher
from app.models import (
    B2BOutboxEvent,
    BlockingReason,
    ProcessedProductEvent,
    ProductModeration,
    ProductModerationBlockingReason,
)


MODERATOR_ID = "11111111-2222-4333-8444-555555555555"
SERVICE_HEADERS = {"X-Service-Key": "dev-service-key"}


class FakeB2BDispatcher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def send_moderation_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


def auth_headers() -> dict[str, str]:
    token = create_access_token({"moderator_id": MODERATOR_ID})
    return {"Authorization": f"Bearer {token}"}


def override_b2b(client: TestClient) -> FakeB2BDispatcher:
    fake = FakeB2BDispatcher()
    client.app.dependency_overrides[get_b2b_dispatcher] = lambda: fake
    return fake


def create_hard_reason(db: Session) -> BlockingReason:
    reason = BlockingReason(
        code=f"COUNTERFEIT_{uuid4().hex.upper()}",
        title="Counterfeit product",
        description="Product cannot return to the catalog",
        hard_block=True,
        is_active=True,
    )
    db.add(reason)
    db.commit()
    db.refresh(reason)
    return reason


def create_review_card(db: Session) -> ProductModeration:
    product_id = str(uuid4())
    card = ProductModeration(
        product_id=product_id,
        seller_id=str(uuid4()),
        category_id=str(uuid4()),
        kind="CREATE",
        status="IN_REVIEW",
        queue_priority=1,
        json_after={
            "id": product_id,
            "title": "Counterfeit phone",
            "description": "Suspicious listing",
            "skus": [
                {
                    "id": str(uuid4()),
                    "name": "Black",
                    "active_quantity": 3,
                }
            ],
        },
        total_active_quantity=3,
        content_revision=1,
        review_revision=1,
        moderator_id=MODERATOR_ID,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def hard_block(
    client: TestClient,
    card: ProductModeration,
    reason: BlockingReason,
) -> Any:
    return client.post(
        f"/api/v1/products/{card.product_id}/decline",
        json={
            "blocking_reason_id": reason.id,
            "moderator_comment": "Confirmed counterfeit",
            "field_reports": [],
        },
        headers=auth_headers(),
    )


def product_event(
    card: ProductModeration,
    event_type: str,
    *,
    key: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"product_id": card.product_id}
    if event_type == "PRODUCT_EDITED":
        payload.update(
            {
                "seller_id": card.seller_id,
                "category_id": card.category_id,
                "json_before": card.json_after,
                "json_after": {
                    **card.json_after,
                    "title": "Seller attempted an edit",
                },
            }
        )
    return {
        "event_type": event_type,
        "idempotency_key": key,
        "occurred_at": "2026-06-11T10:00:00Z",
        "payload": payload,
    }


def test_hard_block_transitions_to_terminal_and_emits_event(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_hard_reason(db_session)
    card = create_review_card(db_session)
    fake = override_b2b(client)

    response = hard_block(client, card, reason)

    db_session.refresh(card)
    outbox = db_session.query(B2BOutboxEvent).one()
    assert response.status_code == 200
    assert response.json()["status"] == "HARD_BLOCKED"
    assert card.status == "HARD_BLOCKED"
    assert card.blocking_reason_id == reason.id
    assert card.decision_at is not None
    assert len(fake.events) == 1
    assert fake.events[0]["event_type"] == "BLOCKED"
    assert outbox.status == "SENT"


def test_hard_block_event_carries_hard_block_true(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_hard_reason(db_session)
    card = create_review_card(db_session)
    fake = override_b2b(client)

    response = client.post(
        f"/api/v1/tickets/{card.id}/block",
        json={
            "blocking_reason_ids": [reason.id],
            "comment": "Copyright violation",
            "field_reports": [],
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert fake.events[0]["event_type"] == "BLOCKED"
    assert fake.events[0]["hard_block"] is True
    assert fake.events[0]["blocking_reason_id"] == reason.id


def test_hard_block_accepts_multiple_reason_ids(
    client: TestClient,
    db_session: Session,
) -> None:
    soft_reason = BlockingReason(
        code=f"MISLEADING_{uuid4().hex.upper()}",
        title="Misleading description",
        hard_block=False,
        is_active=True,
    )
    hard_reason = create_hard_reason(db_session)
    db_session.add(soft_reason)
    db_session.commit()
    db_session.refresh(soft_reason)
    card = create_review_card(db_session)
    fake = override_b2b(client)

    response = client.post(
        f"/api/v1/tickets/{card.id}/block",
        json={
            "blocking_reason_ids": [soft_reason.id, hard_reason.id],
            "comment": "Multiple violations confirmed",
            "field_reports": [],
        },
        headers=auth_headers(),
    )

    db_session.refresh(card)
    stored_reason_ids = {
        row.blocking_reason_id
        for row in db_session.query(ProductModerationBlockingReason)
        .filter(ProductModerationBlockingReason.product_moderation_id == card.id)
        .all()
    }
    assert response.status_code == 200
    assert response.json()["status"] == "HARD_BLOCKED"
    assert card.blocking_reason_id == hard_reason.id
    assert stored_reason_ids == {soft_reason.id, hard_reason.id}
    assert fake.events[0]["hard_block"] is True
    assert fake.events[0]["blocking_reason_id"] == hard_reason.id


def test_any_modify_on_hard_blocked_returns_403(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_hard_reason(db_session)
    card = create_review_card(db_session)
    override_b2b(client)
    assert hard_block(client, card, reason).status_code == 200

    approve_response = client.post(
        f"/api/v1/tickets/{card.id}/approve",
        headers=auth_headers(),
    )
    decline_response = client.post(
        f"/api/v1/products/{card.product_id}/decline",
        json={"blocking_reason_id": reason.id, "field_reports": []},
        headers=auth_headers(),
    )

    assert approve_response.status_code == 403
    assert decline_response.status_code == 403
    assert approve_response.json()["code"] == "HARD_BLOCKED_TERMINAL"
    assert decline_response.json()["code"] == "HARD_BLOCKED_TERMINAL"


def test_edited_event_on_hard_blocked_is_ignored(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_hard_reason(db_session)
    card = create_review_card(db_session)
    override_b2b(client)
    assert hard_block(client, card, reason).status_code == 200
    original_snapshot = dict(card.json_after)
    event = product_event(card, "PRODUCT_EDITED", key=str(uuid4()))

    first_response = client.post(
        "/api/v1/b2b/events",
        json=event,
        headers=SERVICE_HEADERS,
    )
    second_response = client.post(
        "/api/v1/b2b/events",
        json=event,
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(card)
    assert first_response.status_code == 202
    assert second_response.status_code == 202
    assert first_response.json()["status"] == "HARD_BLOCKED"
    assert second_response.json()["duplicate"] is True
    assert card.status == "HARD_BLOCKED"
    assert card.json_after == original_snapshot
    assert db_session.query(ProcessedProductEvent).count() == 1


def test_deleted_event_removes_hard_blocked(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_hard_reason(db_session)
    card = create_review_card(db_session)
    card_id = card.id
    override_b2b(client)
    assert hard_block(client, card, reason).status_code == 200

    response = client.post(
        "/api/v1/b2b/events",
        json=product_event(
            card,
            "PRODUCT_DELETED",
            key=str(uuid4()),
        ),
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 202
    assert response.json()["product_moderation_id"] is None
    assert db_session.get(ProductModeration, card_id) is None
