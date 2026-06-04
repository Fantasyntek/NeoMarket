from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.b2c import get_b2c_dispatcher
from app.models import B2COutboxEvent, Product, ProductFieldReport, SKU


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
SERVICE_HEADERS = {"X-Service-Key": "dev-service-key"}
EVENT_KEY = "11111111-2222-3333-4444-555555555555"
BLOCKING_REASON_ID = "a7b8c9d0-1234-5678-ef01-890123456789"


class FakeB2CDispatcher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def send_product_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


def override_b2c(client: TestClient) -> FakeB2CDispatcher:
    fake = FakeB2CDispatcher()
    client.app.dependency_overrides[get_b2c_dispatcher] = lambda: fake
    return fake


def create_product_fixture(
    db_session: Session,
    status: str = "ON_MODERATION",
    blocked: bool = False,
) -> Product:
    product = Product(
        seller_id=TEST_SELLER_ID,
        category_id=TEST_CATEGORY_ID,
        title="iPhone 15 Pro Max",
        slug="iphone-15-pro-max",
        description="Flagship Apple smartphone",
        status=status,
        blocked=blocked,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def create_sku_fixture(db_session: Session, product: Product) -> SKU:
    sku = SKU(
        product_id=product.id,
        name="256GB Black",
        price=12999000,
        cost_price=9500000,
        discount=0,
        image="/s3/iphone15-black-256.jpg",
        active_quantity=5,
        reserved_quantity=0,
    )
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


def add_blocking_details(db_session: Session, product: Product) -> None:
    product.blocking_reason_id = BLOCKING_REASON_ID
    product.blocking_reason_title = "Old reason"
    product.moderator_comment = "Old comment"
    product.field_reports = [
        ProductFieldReport(
            field_name="description",
            sku_id=None,
            comment="Old field report",
        )
    ]
    db_session.commit()
    db_session.refresh(product)


def blocked_payload(product_id: str, hard_block: bool = False, key: str = EVENT_KEY) -> dict:
    return {
        "idempotency_key": key,
        "product_id": product_id,
        "status": "BLOCKED",
        "hard_block": hard_block,
        "blocking_reason": {
            "id": BLOCKING_REASON_ID,
            "title": "Description does not match product",
            "comment": "Description and photos do not match",
        },
        "field_reports": [
            {
                "field_name": "description",
                "sku_id": None,
                "comment": "Description was copied from another product",
            }
        ],
    }


def product_update_payload() -> dict:
    return {
        "title": "Updated iPhone",
        "description": "Updated description",
        "category_id": TEST_CATEGORY_ID,
        "images": [{"url": "/s3/updated.jpg", "ordering": 0}],
        "characteristics": [{"name": "Brand", "value": "Apple"}],
    }


def test_moderated_event_clears_blocking_data(
    client: TestClient,
    db_session: Session,
) -> None:
    product = create_product_fixture(db_session, status="BLOCKED", blocked=True)
    add_blocking_details(db_session, product)
    override_b2c(client)

    response = client.post(
        "/api/v1/events/moderation",
        json={
            "idempotency_key": EVENT_KEY,
            "product_id": product.id,
            "status": "MODERATED",
        },
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(product)
    assert response.status_code == 200
    assert product.status == "MODERATED"
    assert product.blocked is False
    assert product.blocking_reason_id is None
    assert product.blocking_reason_title is None
    assert product.moderator_comment is None
    assert product.field_reports == []


def test_blocked_soft_saves_field_reports(
    client: TestClient,
    db_session: Session,
) -> None:
    product = create_product_fixture(db_session)
    sku = create_sku_fixture(db_session, product)
    fake_b2c = override_b2c(client)

    response = client.post(
        "/api/v1/events/moderation",
        json=blocked_payload(product.id, hard_block=False),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(product)
    assert response.status_code == 200
    assert product.status == "BLOCKED"
    assert product.blocked is True
    assert product.blocking_reason_title == "Description does not match product"
    assert len(product.field_reports) == 1
    assert product.field_reports[0].field_name == "description"
    assert len(fake_b2c.events) == 1
    assert fake_b2c.events[0]["event"] == "PRODUCT_BLOCKED"
    assert fake_b2c.events[0]["product_id"] == product.id
    assert fake_b2c.events[0]["sku_ids"] == [sku.id]
    assert db_session.query(B2COutboxEvent).one().status == "SENT"


def test_blocked_hard_sets_terminal_status(
    client: TestClient,
    db_session: Session,
) -> None:
    product = create_product_fixture(db_session)
    create_sku_fixture(db_session, product)
    fake_b2c = override_b2c(client)

    response = client.post(
        "/api/v1/events/moderation",
        json=blocked_payload(product.id, hard_block=True),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(product)
    assert response.status_code == 200
    assert product.status == "HARD_BLOCKED"
    assert product.blocked is True
    assert len(fake_b2c.events) == 1
    assert fake_b2c.events[0]["event"] == "PRODUCT_BLOCKED"


def test_hard_blocked_product_rejects_seller_edits(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    product = create_product_fixture(db_session, status="HARD_BLOCKED", blocked=True)

    put_response = client.put(
        f"/api/v1/products/{product.id}",
        json=product_update_payload(),
        headers=auth_headers,
    )
    delete_response = client.delete(f"/api/v1/products/{product.id}", headers=auth_headers)

    assert put_response.status_code == 403
    assert delete_response.status_code == 403


def test_duplicate_event_same_idempotency_key_no_side_effects(
    client: TestClient,
    db_session: Session,
) -> None:
    product = create_product_fixture(db_session)
    create_sku_fixture(db_session, product)
    fake_b2c = override_b2c(client)
    payload = blocked_payload(product.id, hard_block=False)

    first_response = client.post(
        "/api/v1/events/moderation",
        json=payload,
        headers=SERVICE_HEADERS,
    )
    second_response = client.post(
        "/api/v1/events/moderation",
        json={**payload, "hard_block": True},
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(product)
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert product.status == "BLOCKED"
    assert len(product.field_reports) == 1
    assert len(fake_b2c.events) == 1
    assert db_session.query(B2COutboxEvent).count() == 1


def test_missing_service_key_returns_401(
    client: TestClient,
    db_session: Session,
) -> None:
    product = create_product_fixture(db_session)

    response = client.post(
        "/api/v1/events/moderation",
        json={
            "idempotency_key": EVENT_KEY,
            "product_id": product.id,
            "status": "MODERATED",
        },
    )

    assert response.status_code == 401
    assert response.json() == {"code": "UNAUTHORIZED", "message": "Invalid service key"}
