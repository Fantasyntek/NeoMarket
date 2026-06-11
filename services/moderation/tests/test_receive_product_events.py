from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ProcessedProductEvent, ProductModeration


PRODUCT_ID = "a1b2c3d4-e5f6-4890-abcd-ef1234567890"
SELLER_ID = "c3d4e5f6-a7b8-4012-8def-123456789012"
CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
SERVICE_HEADERS = {"X-Service-Key": "dev-service-key"}
CREATED_KEY = "11111111-2222-4333-8444-555555555555"
EDITED_KEY = "22222222-3333-4444-8555-666666666666"
DELETED_KEY = "33333333-4444-4555-8666-777777777777"


def product_snapshot(
    *,
    title: str = "iPhone 15 Pro Max",
    active_quantity: int = 5,
) -> dict[str, Any]:
    return {
        "id": PRODUCT_ID,
        "seller_id": SELLER_ID,
        "category_id": CATEGORY_ID,
        "title": title,
        "description": "Flagship Apple smartphone",
        "status": "ON_MODERATION",
        "deleted": False,
        "images": [{"url": "/s3/iphone.jpg", "ordering": 0}],
        "skus": [
            {
                "id": "660e8400-e29b-41d4-a716-446655440001",
                "name": "256GB Black",
                "price": 12999000,
                "active_quantity": active_quantity,
            }
        ],
    }


def openapi_event(
    event_type: str,
    *,
    key: str,
    json_before: dict[str, Any] | None = None,
    json_after: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"product_id": PRODUCT_ID}
    if event_type != "PRODUCT_DELETED":
        payload.update(
            {
                "seller_id": SELLER_ID,
                "category_id": CATEGORY_ID,
                "json_after": json_after or product_snapshot(),
            }
        )
    if event_type == "PRODUCT_EDITED":
        payload["json_before"] = json_before or product_snapshot(title="Old title")
    return {
        "event_type": event_type,
        "idempotency_key": key,
        "occurred_at": "2026-06-09T10:00:00Z",
        "payload": payload,
    }


def create_card(
    db_session: Session,
    *,
    status: str,
    moderator_id: str | None = None,
    queue_priority: int = 1,
) -> ProductModeration:
    snapshot = product_snapshot(title="Old title")
    card = ProductModeration(
        product_id=PRODUCT_ID,
        seller_id=SELLER_ID,
        category_id=CATEGORY_ID,
        status=status,
        queue_priority=queue_priority,
        json_after=snapshot,
        total_active_quantity=5,
        moderator_id=moderator_id,
        blocking_reason_id=(
            "88888888-7777-4666-8555-444444444444"
            if status == "BLOCKED"
            else None
        ),
        moderator_comment="Fix product" if status == "BLOCKED" else None,
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


def test_created_pending(
    client: TestClient,
    db_session: Session,
) -> None:
    response = client.post(
        "/api/v1/b2b/events",
        json=openapi_event("PRODUCT_CREATED", key=CREATED_KEY),
        headers=SERVICE_HEADERS,
    )

    card = db_session.query(ProductModeration).one()
    assert response.status_code == 202
    assert response.json()["status"] == "PENDING"
    assert card.status == "PENDING"
    assert card.queue_priority == 1
    assert card.json_before is None
    assert card.json_after["title"] == "iPhone 15 Pro Max"
    assert card.total_active_quantity == 5


def test_edited_returns_to_review(
    client: TestClient,
    db_session: Session,
) -> None:
    card = create_card(
        db_session,
        status="BLOCKED",
        moderator_id="99999999-8888-4777-8666-555555555555",
    )
    before = deepcopy(card.json_after)
    after = product_snapshot(title="Fixed title")

    response = client.post(
        "/api/v1/b2b/events",
        json=openapi_event(
            "PRODUCT_EDITED",
            key=EDITED_KEY,
            json_before=before,
            json_after=after,
        ),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(card)
    assert response.status_code == 202
    assert card.status == "PENDING"
    assert card.queue_priority == 2
    assert card.moderator_id is None
    assert card.blocking_reason_id is None
    assert card.moderator_comment is None
    assert card.json_before["title"] == "Old title"
    assert card.json_after["title"] == "Fixed title"


def test_edited_approved_returns_to_review_with_stock_priority(
    client: TestClient,
    db_session: Session,
) -> None:
    card = create_card(db_session, status="APPROVED")

    response = client.post(
        "/api/v1/b2b/events",
        json=openapi_event(
            "PRODUCT_EDITED",
            key=EDITED_KEY,
            json_after=product_snapshot(title="Edited", active_quantity=0),
        ),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(card)
    assert response.status_code == 202
    assert card.status == "PENDING"
    assert card.queue_priority == 4


def test_edited_updates_in_review(
    client: TestClient,
    db_session: Session,
) -> None:
    moderator_id = "99999999-8888-4777-8666-555555555555"
    card = create_card(
        db_session,
        status="IN_REVIEW",
        moderator_id=moderator_id,
        queue_priority=3,
    )

    response = client.post(
        "/api/v1/b2b/events",
        json=openapi_event(
            "PRODUCT_EDITED",
            key=EDITED_KEY,
            json_after=product_snapshot(title="Changed during review"),
        ),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(card)
    assert response.status_code == 202
    assert card.status == "IN_REVIEW"
    assert card.moderator_id == moderator_id
    assert card.queue_priority == 3
    assert card.json_after["title"] == "Changed during review"


def test_deleted_archived(
    client: TestClient,
    db_session: Session,
) -> None:
    card = create_card(
        db_session,
        status="IN_REVIEW",
        moderator_id="99999999-8888-4777-8666-555555555555",
    )

    response = client.post(
        "/api/v1/b2b/events",
        json=openapi_event("PRODUCT_DELETED", key=DELETED_KEY),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(card)
    assert response.status_code == 202
    assert card.status == "ARCHIVED"
    assert card.archived is True
    assert card.archived_at is not None
    assert card.moderator_id is None


def test_duplicate_event_no_side_effects(
    client: TestClient,
    db_session: Session,
) -> None:
    payload = openapi_event("PRODUCT_CREATED", key=CREATED_KEY)
    first_response = client.post(
        "/api/v1/b2b/events",
        json=payload,
        headers=SERVICE_HEADERS,
    )
    card = db_session.query(ProductModeration).one()
    original_title = card.json_after["title"]

    duplicate_payload = deepcopy(payload)
    duplicate_payload["payload"]["json_after"]["title"] = "Must be ignored"
    second_response = client.post(
        "/api/v1/b2b/events",
        json=duplicate_payload,
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(card)
    assert first_response.status_code == 202
    assert second_response.status_code == 202
    assert second_response.json()["duplicate"] is True
    assert card.json_after["title"] == original_title
    assert db_session.query(ProductModeration).count() == 1
    assert db_session.query(ProcessedProductEvent).count() == 1


def test_missing_service_header_401(client: TestClient) -> None:
    response = client.post(
        "/api/v1/b2b/events",
        json=openapi_event("PRODUCT_CREATED", key=CREATED_KEY),
    )

    assert response.status_code == 401
    assert response.json() == {
        "code": "UNAUTHORIZED",
        "message": "Invalid service key",
    }


def test_canonical_events_product_alias_accepts_action_names(
    client: TestClient,
) -> None:
    snapshot = product_snapshot()
    response = client.post(
        "/api/v1/events/product",
        json={
            "event": "CREATED",
            "idempotency_key": CREATED_KEY,
            "date": "2026-06-09T10:00:00Z",
            "product_id": PRODUCT_ID,
            "seller_id": SELLER_ID,
            "category_id": CATEGORY_ID,
            "json_after": snapshot,
        },
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "PENDING"
