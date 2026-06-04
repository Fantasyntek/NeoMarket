from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.b2c import get_b2c_dispatcher
from app.moderation import get_moderation_dispatcher
from app.models import B2COutboxEvent, ModerationOutboxEvent, Product, SKU


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"


class FakeModerationDispatcher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def send_product_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


class FakeB2CDispatcher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def send_product_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


def override_moderation(client: TestClient) -> FakeModerationDispatcher:
    fake = FakeModerationDispatcher()
    client.app.dependency_overrides[get_moderation_dispatcher] = lambda: fake
    return fake


def override_b2c(client: TestClient) -> FakeB2CDispatcher:
    fake = FakeB2CDispatcher()
    client.app.dependency_overrides[get_b2c_dispatcher] = lambda: fake
    return fake


def create_product_fixture(
    db_session: Session,
    status: str = "MODERATED",
) -> Product:
    product = Product(
        seller_id=TEST_SELLER_ID,
        category_id=TEST_CATEGORY_ID,
        title="iPhone 15 Pro Max",
        slug="iphone-15-pro-max",
        description="Flagship Apple smartphone",
        status=status,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def create_sku_fixture(
    db_session: Session,
    product: Product,
    active_quantity: int = 0,
    reserved_quantity: int = 0,
    name: str = "256GB Black",
) -> SKU:
    sku = SKU(
        product_id=product.id,
        name=name,
        price=12999000,
        cost_price=9500000,
        discount=0,
        image="/s3/iphone15-black-256.jpg",
        active_quantity=active_quantity,
        reserved_quantity=reserved_quantity,
    )
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


def test_delete_sku_succeeds(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    product = create_product_fixture(db_session, status="CREATED")
    sku = create_sku_fixture(db_session, product)
    override_moderation(client)
    override_b2c(client)

    response = client.delete(f"/api/v1/skus/{sku.id}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert db_session.get(SKU, sku.id) is None


def test_delete_sku_with_active_reserves_returns_409(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    product = create_product_fixture(db_session, status="MODERATED")
    sku = create_sku_fixture(db_session, product, reserved_quantity=2)
    override_moderation(client)
    override_b2c(client)

    response = client.delete(f"/api/v1/skus/{sku.id}", headers=auth_headers)

    assert response.status_code == 409
    assert response.json() == {
        "code": "CONFLICT",
        "message": "Cannot delete SKU with active reserves",
    }
    assert db_session.get(SKU, sku.id) is not None


def test_last_sku_on_moderation_transitions_product_to_created(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    product = create_product_fixture(db_session, status="ON_MODERATION")
    sku = create_sku_fixture(db_session, product)
    fake_moderation = override_moderation(client)
    override_b2c(client)

    response = client.delete(f"/api/v1/skus/{sku.id}", headers=auth_headers)

    db_session.refresh(product)
    assert response.status_code == 200
    assert product.status == "CREATED"
    assert db_session.get(SKU, sku.id) is None
    assert len(fake_moderation.events) == 1
    assert fake_moderation.events[0]["event"] == "DELETED"
    assert fake_moderation.events[0]["product_id"] == product.id
    assert db_session.query(ModerationOutboxEvent).one().status == "SENT"


def test_delete_sku_hard_blocked_product_returns_403(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    product = create_product_fixture(db_session, status="HARD_BLOCKED")
    sku = create_sku_fixture(db_session, product, reserved_quantity=2)
    override_moderation(client)
    override_b2c(client)

    response = client.delete(f"/api/v1/skus/{sku.id}", headers=auth_headers)

    assert response.status_code == 403
    assert response.json() == {
        "code": "FORBIDDEN",
        "message": "Cannot delete SKU of hard-blocked product",
    }
    assert db_session.get(SKU, sku.id) is not None


def test_sku_out_of_stock_event_on_moderated_product(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    product = create_product_fixture(db_session, status="MODERATED")
    sku = create_sku_fixture(db_session, product, active_quantity=5)
    override_moderation(client)
    fake_b2c = override_b2c(client)

    response = client.delete(f"/api/v1/skus/{sku.id}", headers=auth_headers)

    assert response.status_code == 200
    assert db_session.get(SKU, sku.id) is None
    assert len(fake_b2c.events) == 1
    event = fake_b2c.events[0]
    assert event["event"] == "SKU_OUT_OF_STOCK"
    assert event["product_id"] == product.id
    assert event["sku_id"] == sku.id
    assert db_session.query(B2COutboxEvent).one().status == "SENT"
