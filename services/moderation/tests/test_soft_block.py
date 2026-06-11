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
    ModerationFieldReport,
    ProductModeration,
)


MODERATOR_ID = "11111111-2222-4333-8444-555555555555"
OTHER_MODERATOR_ID = "22222222-3333-4444-8555-666666666666"


class FakeB2BDispatcher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def send_moderation_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


def headers(moderator_id: str = MODERATOR_ID) -> dict[str, str]:
    token = create_access_token({"moderator_id": moderator_id})
    return {"Authorization": f"Bearer {token}"}


def override_b2b(client: TestClient) -> FakeB2BDispatcher:
    fake = FakeB2BDispatcher()
    client.app.dependency_overrides[get_b2b_dispatcher] = lambda: fake
    return fake


def create_reason(
    db: Session,
    *,
    hard_block: bool = False,
    is_active: bool = True,
) -> BlockingReason:
    reason = BlockingReason(
        code=f"REASON_{uuid4().hex.upper()}",
        title="Product content needs correction",
        description="Seller can fix the product",
        hard_block=hard_block,
        is_active=is_active,
    )
    db.add(reason)
    db.commit()
    db.refresh(reason)
    return reason


def create_card(
    db: Session,
    *,
    moderator_id: str = MODERATOR_ID,
) -> ProductModeration:
    card = ProductModeration(
        product_id=str(uuid4()),
        seller_id=str(uuid4()),
        category_id=str(uuid4()),
        kind="CREATE",
        status="IN_REVIEW",
        queue_priority=1,
        json_after={
            "title": "iPhone",
            "description": "Old description",
            "skus": [{"id": str(uuid4()), "name": "Black"}],
        },
        content_revision=1,
        review_revision=1,
        moderator_id=moderator_id,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def canonical_payload(reason_id: str) -> dict[str, Any]:
    return {
        "blocking_reason_id": reason_id,
        "moderator_comment": "Correct the copied description and photo",
        "field_reports": [
            {
                "field_name": "description",
                "comment": "Description belongs to another product",
            },
            {
                "field_name": "product_images",
                "comment": "Image is blurred",
                "severity": "WARNING",
            },
        ],
    }


def test_soft_block_transitions_to_blocked_with_field_reports(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_reason(db_session)
    card = create_card(db_session)
    override_b2b(client)

    response = client.post(
        f"/api/v1/products/{card.product_id}/decline",
        json=canonical_payload(reason.id),
        headers=headers(),
    )

    db_session.refresh(card)
    reports = (
        db_session.query(ModerationFieldReport)
        .filter(ModerationFieldReport.product_moderation_id == card.id)
        .order_by(ModerationFieldReport.field_name)
        .all()
    )
    assert response.status_code == 200
    assert response.json()["status"] == "BLOCKED"
    assert card.status == "BLOCKED"
    assert card.blocking_reason_id == reason.id
    assert card.moderator_comment == "Correct the copied description and photo"
    assert card.decision_at is not None
    assert [(item.field_name, item.severity) for item in reports] == [
        ("description", "ERROR"),
        ("product_images", "WARNING"),
    ]


def test_soft_block_emits_event_to_b2b(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_reason(db_session)
    card = create_card(db_session)
    fake = override_b2b(client)

    response = client.post(
        f"/api/v1/tickets/{card.id}/block",
        json={
            "blocking_reason_ids": [reason.id],
            "comment": "Fix description",
            "field_reports": [
                {
                    "field_path": "description",
                    "message": "Description is copied",
                    "severity": "ERROR",
                }
            ],
        },
        headers=headers(),
    )

    outbox = db_session.query(B2BOutboxEvent).one()
    assert response.status_code == 200
    assert len(fake.events) == 1
    assert fake.events[0]["event_type"] == "BLOCKED"
    assert fake.events[0]["hard_block"] is False
    assert fake.events[0]["blocking_reason_id"] == reason.id
    assert fake.events[0]["field_reports"] == [
        {
            "field_name": "description",
            "sku_id": None,
            "comment": "Description is copied",
        }
    ]
    assert outbox.status == "SENT"


def test_openapi_field_path_is_normalized_to_canonical_enum(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_reason(db_session)
    card = create_card(db_session)
    fake = override_b2b(client)

    response = client.post(
        f"/api/v1/tickets/{card.id}/block",
        json={
            "blocking_reason_ids": [reason.id],
            "field_reports": [
                {
                    "field_path": "images[0].url",
                    "message": "Image is blurred",
                },
                {
                    "field_path": "skus[0].price",
                    "message": "Price requires verification",
                },
            ],
        },
        headers=headers(),
    )

    assert response.status_code == 200
    assert [item["field_name"] for item in fake.events[0]["field_reports"]] == [
        "product_images",
        "sku_price",
    ]


def test_soft_block_unknown_reason_returns_400(
    client: TestClient,
    db_session: Session,
) -> None:
    card = create_card(db_session)
    fake = override_b2b(client)

    response = client.post(
        f"/api/v1/tickets/{card.id}/block",
        json=canonical_payload(str(uuid4())),
        headers=headers(),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "UNKNOWN_BLOCKING_REASON"
    assert fake.events == []


def test_soft_block_others_card_returns_403(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_reason(db_session)
    card = create_card(db_session, moderator_id=OTHER_MODERATOR_ID)
    fake = override_b2b(client)

    response = client.post(
        f"/api/v1/tickets/{card.id}/block",
        json=canonical_payload(reason.id),
        headers=headers(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "CARD_OWNED_BY_ANOTHER_MODERATOR"
    assert fake.events == []


def test_soft_block_invalid_field_name_returns_400(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_reason(db_session)
    card = create_card(db_session)
    fake = override_b2b(client)
    payload = canonical_payload(reason.id)
    payload["field_reports"][0]["field_name"] = "seller_password"

    response = client.post(
        f"/api/v1/tickets/{card.id}/block",
        json=payload,
        headers=headers(),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_FIELD_NAME"
    assert fake.events == []


def test_hard_only_reason_routes_to_hard_block(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_reason(db_session, hard_block=True)
    card = create_card(db_session)
    fake = override_b2b(client)

    response = client.post(
        f"/api/v1/tickets/{card.id}/block",
        json=canonical_payload(reason.id),
        headers=headers(),
    )

    db_session.refresh(card)
    assert response.status_code == 200
    assert response.json()["status"] == "HARD_BLOCKED"
    assert card.status == "HARD_BLOCKED"
    assert fake.events[0]["event_type"] == "BLOCKED"
    assert fake.events[0]["hard_block"] is True


def test_edited_after_soft_block_clears_field_reports(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_reason(db_session)
    card = create_card(db_session)
    override_b2b(client)
    blocked = client.post(
        f"/api/v1/products/{card.product_id}/decline",
        json=canonical_payload(reason.id),
        headers=headers(),
    )

    edited = client.post(
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
                "json_after": {**card.json_after, "description": "Fixed"},
            },
        },
        headers={"X-Service-Key": "dev-service-key"},
    )

    db_session.refresh(card)
    assert blocked.status_code == 200
    assert edited.status_code == 202
    assert card.status == "PENDING"
    assert card.blocking_reason_id is None
    assert (
        db_session.query(ModerationFieldReport)
        .filter(ModerationFieldReport.product_moderation_id == card.id)
        .count()
        == 0
    )
