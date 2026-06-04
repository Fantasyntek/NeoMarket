from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.b2c import get_b2c_dispatcher
from app.models import B2COutboxEvent, ModerationOutboxEvent, Product, SKU
from app.moderation import get_moderation_dispatcher


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
OTHER_SELLER_ID = "11111111-1111-1111-1111-111111111111"


class FakeProductEventDispatcher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def send_product_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


def override_dispatchers(
    client: TestClient,
) -> tuple[FakeProductEventDispatcher, FakeProductEventDispatcher]:
    fake_moderation = FakeProductEventDispatcher()
    fake_b2c = FakeProductEventDispatcher()
    client.app.dependency_overrides[get_moderation_dispatcher] = lambda: fake_moderation
    client.app.dependency_overrides[get_b2c_dispatcher] = lambda: fake_b2c
    return fake_moderation, fake_b2c


def create_product_fixture(
    db_session: Session,
    seller_id: str = TEST_SELLER_ID,
    deleted: bool = False,
    title: str = "iPhone 15 Pro Max",
) -> Product:
    product = Product(
        seller_id=seller_id,
        category_id=TEST_CATEGORY_ID,
        title=title,
        slug=title.lower().replace(" ", "-"),
        description="Flagship Apple smartphone",
        status="MODERATED",
        deleted=deleted,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def create_sku_fixture(db_session: Session, product: Product, name: str) -> SKU:
    sku = SKU(
        product_id=product.id,
        name=name,
        price=12999000,
        cost_price=9500000,
        discount=0,
        image=f"/s3/{name.lower().replace(' ', '-')}.jpg",
        active_quantity=3,
        reserved_quantity=1,
    )
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


def test_delete_sets_deleted_true(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session)
    override_dispatchers(client)

    response = client.delete(f"/api/v1/products/{product.id}", headers=auth_headers)

    db_session.refresh(product)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert product.deleted is True


def test_delete_emits_event_to_moderation(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session)
    fake_moderation, _ = override_dispatchers(client)

    response = client.delete(f"/api/v1/products/{product.id}", headers=auth_headers)

    assert response.status_code == 200
    assert len(fake_moderation.events) == 1
    event = fake_moderation.events[0]
    assert event["event"] == "DELETED"
    assert event["product_id"] == product.id
    assert event["seller_id"] == TEST_SELLER_ID
    assert event["idempotency_key"]
    assert event["date"].endswith("Z")
    assert db_session.query(ModerationOutboxEvent).one().status == "SENT"


def test_delete_emits_product_deleted_to_b2c(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session)
    sku_1 = create_sku_fixture(db_session, product, "256GB Black")
    sku_2 = create_sku_fixture(db_session, product, "512GB Blue")
    _, fake_b2c = override_dispatchers(client)

    response = client.delete(f"/api/v1/products/{product.id}", headers=auth_headers)

    assert response.status_code == 200
    assert len(fake_b2c.events) == 1
    event = fake_b2c.events[0]
    assert event["event"] == "PRODUCT_DELETED"
    assert event["product_id"] == product.id
    assert event["sku_ids"] == [sku_1.id, sku_2.id]
    assert event["idempotency_key"]
    assert event["date"].endswith("Z")
    assert db_session.query(B2COutboxEvent).one().status == "SENT"


def test_delete_already_deleted_returns_400(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session, deleted=True)
    override_dispatchers(client)

    response = client.delete(f"/api/v1/products/{product.id}", headers=auth_headers)

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "Product already deleted",
    }
    assert db_session.query(ModerationOutboxEvent).count() == 0
    assert db_session.query(B2COutboxEvent).count() == 0


def test_delete_others_product_returns_403(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session, seller_id=OTHER_SELLER_ID)
    override_dispatchers(client)
    auth_headers = {
        "Authorization": f"Bearer {create_access_token({'seller_id': TEST_SELLER_ID})}"
    }

    response = client.delete(f"/api/v1/products/{product.id}", headers=auth_headers)

    assert response.status_code == 403
    assert response.json() == {
        "code": "NOT_OWNER",
        "message": "Product does not belong to the authenticated seller",
    }
    db_session.refresh(product)
    assert product.deleted is False


def test_deleted_product_not_in_seller_list(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    deleted_product = create_product_fixture(
        db_session,
        deleted=True,
        title="Deleted iPhone",
    )
    visible_product = create_product_fixture(
        db_session,
        title="Visible iPhone",
    )

    response = client.get("/api/v1/products", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    item_ids = [item["id"] for item in body["items"]]
    assert visible_product.id in item_ids
    assert deleted_product.id not in item_ids
    assert body["total_count"] == 1
