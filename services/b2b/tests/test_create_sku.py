from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.moderation import get_moderation_dispatcher
from app.models import ModerationOutboxEvent, Product, SKU


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"


class FakeModerationDispatcher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def send_product_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


def create_product_fixture(db_session: Session, status: str = "CREATED") -> Product:
    product = Product(
        seller_id=TEST_SELLER_ID,
        category_id=TEST_CATEGORY_ID,
        title="iPhone 15 Pro Max",
        slug="iphone-15-pro-max",
        description="Флагманский смартфон Apple",
        status=status,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def valid_sku_payload(product_id: str) -> dict:
    return {
        "product_id": product_id,
        "name": "256GB Black",
        "price": 12999000,
        "cost_price": 9500000,
        "discount": 0,
        "image": "/s3/iphone15-black-256.jpg",
        "characteristics": [
            {"name": "Цвет", "value": "Чёрный"},
            {"name": "Объём памяти", "value": "256 ГБ"},
        ],
    }


def override_moderation(client: TestClient) -> FakeModerationDispatcher:
    fake = FakeModerationDispatcher()
    client.app.dependency_overrides[get_moderation_dispatcher] = lambda: fake
    return fake


def test_first_sku_transitions_product_to_on_moderation(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session)
    override_moderation(client)

    response = client.post(
        "/api/v1/skus", json=valid_sku_payload(product.id), headers=auth_headers
    )

    db_session.refresh(product)
    assert response.status_code == 201
    assert response.json()["product_id"] == product.id
    assert product.status == "ON_MODERATION"


def test_first_sku_emits_created_event_to_moderation(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session)
    fake_moderation = override_moderation(client)

    response = client.post(
        "/api/v1/skus", json=valid_sku_payload(product.id), headers=auth_headers
    )

    assert response.status_code == 201
    assert len(fake_moderation.events) == 1
    event = fake_moderation.events[0]
    assert event["event"] == "CREATED"
    assert event["product_id"] == product.id
    assert event["seller_id"] == TEST_SELLER_ID
    assert event["idempotency_key"]
    assert event["date"].endswith("Z")

    outbox_event = db_session.query(ModerationOutboxEvent).one()
    assert outbox_event.status == "SENT"
    assert outbox_event.idempotency_key == event["idempotency_key"]


def test_second_sku_no_state_change(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session, status="ON_MODERATION")
    first_sku = SKU(
        product_id=product.id,
        name="128GB Black",
        price=10999000,
        cost_price=8000000,
        discount=0,
        image="/s3/iphone15-black-128.jpg",
    )
    db_session.add(first_sku)
    db_session.commit()
    fake_moderation = override_moderation(client)

    response = client.post(
        "/api/v1/skus", json=valid_sku_payload(product.id), headers=auth_headers
    )

    db_session.refresh(product)
    assert response.status_code == 201
    assert product.status == "ON_MODERATION"
    assert fake_moderation.events == []
    assert db_session.query(ModerationOutboxEvent).count() == 0


def test_add_sku_to_hard_blocked_returns_403(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session, status="HARD_BLOCKED")
    override_moderation(client)

    response = client.post(
        "/api/v1/skus", json=valid_sku_payload(product.id), headers=auth_headers
    )

    assert response.status_code == 403
    assert response.json() == {
        "code": "FORBIDDEN",
        "message": "Cannot add SKU to hard-blocked product",
    }


def test_missing_image_returns_400(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session)
    payload = valid_sku_payload(product.id)
    payload.pop("image")

    response = client.post("/api/v1/skus", json=payload, headers=auth_headers)

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "image is required",
    }

