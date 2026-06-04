from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.b2c import get_b2c_dispatcher
from app.models import B2COutboxEvent, Product, ReserveOperation, SKU


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
SERVICE_HEADERS = {"X-Service-Key": "dev-service-key"}
RESERVE_KEY = "11111111-2222-3333-4444-555555555555"
ORDER_ID = "66666666-7777-8888-9999-000000000000"


class FakeB2CDispatcher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def send_product_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


def override_b2c(client: TestClient) -> FakeB2CDispatcher:
    fake = FakeB2CDispatcher()
    client.app.dependency_overrides[get_b2c_dispatcher] = lambda: fake
    return fake


def create_product_fixture(db_session: Session) -> Product:
    product = Product(
        seller_id=TEST_SELLER_ID,
        category_id=TEST_CATEGORY_ID,
        title="iPhone 15 Pro Max",
        slug="iphone-15-pro-max",
        description="Flagship Apple smartphone",
        status="MODERATED",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def create_sku_fixture(
    db_session: Session,
    product: Product,
    active_quantity: int,
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


def reserve_payload(sku: SKU, quantity: int, key: str = RESERVE_KEY) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "items": [{"sku_id": sku.id, "quantity": quantity}],
    }


def test_reserve_all_skus_succeeds(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session)
    sku_1 = create_sku_fixture(db_session, product, active_quantity=5, name="256GB Black")
    sku_2 = create_sku_fixture(db_session, product, active_quantity=4, name="512GB Blue")
    override_b2c(client)

    response = client.post(
        "/api/v1/reserve",
        json={
            "idempotency_key": RESERVE_KEY,
            "items": [
                {"sku_id": sku_1.id, "quantity": 2},
                {"sku_id": sku_2.id, "quantity": 1},
            ],
        },
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(sku_1)
    db_session.refresh(sku_2)
    assert response.status_code == 200
    assert response.json() == {
        "reserved": True,
        "items": [
            {"sku_id": sku_1.id, "reserved_quantity": 2, "remaining_stock": 3},
            {"sku_id": sku_2.id, "reserved_quantity": 1, "remaining_stock": 3},
        ],
    }
    assert sku_1.active_quantity == 3
    assert sku_1.reserved_quantity == 2
    assert sku_2.active_quantity == 3
    assert sku_2.reserved_quantity == 1


def test_partial_insufficient_stock_returns_409_all_rollback(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session)
    enough_sku = create_sku_fixture(db_session, product, active_quantity=5)
    low_stock_sku = create_sku_fixture(
        db_session,
        product,
        active_quantity=1,
        name="512GB Blue",
    )
    override_b2c(client)

    response = client.post(
        "/api/v1/reserve",
        json={
            "idempotency_key": RESERVE_KEY,
            "items": [
                {"sku_id": enough_sku.id, "quantity": 2},
                {"sku_id": low_stock_sku.id, "quantity": 3},
            ],
        },
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(enough_sku)
    db_session.refresh(low_stock_sku)
    assert response.status_code == 409
    assert response.json() == {
        "reserved": False,
        "failed_items": [
            {
                "sku_id": low_stock_sku.id,
                "requested": 3,
                "available": 1,
                "reason": "INSUFFICIENT_STOCK",
            }
        ],
    }
    assert enough_sku.active_quantity == 5
    assert enough_sku.reserved_quantity == 0
    assert low_stock_sku.active_quantity == 1
    assert low_stock_sku.reserved_quantity == 0
    assert db_session.query(ReserveOperation).count() == 0


def test_idempotent_reserve_returns_200_without_double_deduction(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session)
    sku = create_sku_fixture(db_session, product, active_quantity=5)
    override_b2c(client)

    first_response = client.post(
        "/api/v1/reserve",
        json=reserve_payload(sku, quantity=2),
        headers=SERVICE_HEADERS,
    )
    second_response = client.post(
        "/api/v1/reserve",
        json=reserve_payload(sku, quantity=2),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(sku)
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()
    assert sku.active_quantity == 3
    assert sku.reserved_quantity == 2


def test_sku_out_of_stock_event_emitted(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session)
    sku = create_sku_fixture(db_session, product, active_quantity=2)
    fake_b2c = override_b2c(client)

    response = client.post(
        "/api/v1/reserve",
        json=reserve_payload(sku, quantity=2),
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 200
    assert len(fake_b2c.events) == 1
    event = fake_b2c.events[0]
    assert event["event"] == "SKU_OUT_OF_STOCK"
    assert event["product_id"] == product.id
    assert event["sku_id"] == sku.id
    assert event["idempotency_key"]
    assert event["date"].endswith("Z")
    assert db_session.query(B2COutboxEvent).one().status == "SENT"


def test_unreserve_restores_quantities(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session)
    sku = create_sku_fixture(
        db_session,
        product,
        active_quantity=3,
        reserved_quantity=2,
    )

    response = client.post(
        "/api/v1/unreserve",
        json={"order_id": ORDER_ID, "items": [{"sku_id": sku.id, "quantity": 2}]},
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(sku)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert sku.active_quantity == 5
    assert sku.reserved_quantity == 0
