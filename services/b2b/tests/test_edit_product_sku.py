from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.moderation import get_moderation_dispatcher
from app.models import ModerationOutboxEvent, Product, SKU


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
OTHER_SELLER_ID = "11111111-1111-1111-1111-111111111111"


class FakeModerationDispatcher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def send_product_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


def override_moderation(client: TestClient) -> FakeModerationDispatcher:
    fake = FakeModerationDispatcher()
    client.app.dependency_overrides[get_moderation_dispatcher] = lambda: fake
    return fake


def create_product_fixture(
    db_session: Session,
    status: str = "MODERATED",
    seller_id: str = TEST_SELLER_ID,
) -> Product:
    product = Product(
        seller_id=seller_id,
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
    reserved_quantity: int = 0,
) -> SKU:
    sku = SKU(
        product_id=product.id,
        name="256GB Black",
        price=12999000,
        cost_price=9500000,
        discount=0,
        image="/s3/iphone15-black-256.jpg",
        active_quantity=3,
        reserved_quantity=reserved_quantity,
    )
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


def product_update_payload() -> dict:
    return {
        "title": "iPhone 15 Pro Max Updated",
        "description": "Updated product description",
        "category_id": TEST_CATEGORY_ID,
        "images": [{"url": "/s3/iphone15-front-v2.jpg", "ordering": 0}],
        "characteristics": [{"name": "Brand", "value": "Apple"}],
    }


def sku_update_payload() -> dict:
    return {
        "name": "256GB Black Titanium",
        "price": 13499000,
        "cost_price": 9800000,
        "discount": 500000,
        "image": "/s3/iphone15-black-titanium.jpg",
        "characteristics": [{"name": "Color", "value": "Black Titanium"}],
    }


def test_edit_moderated_product_returns_to_on_moderation(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session, status="MODERATED")
    fake_moderation = override_moderation(client)

    response = client.put(
        f"/api/v1/products/{product.id}",
        json=product_update_payload(),
        headers=auth_headers,
    )

    db_session.refresh(product)
    assert response.status_code == 200
    assert response.json()["status"] == "ON_MODERATION"
    assert product.status == "ON_MODERATION"
    assert len(fake_moderation.events) == 1
    assert fake_moderation.events[0]["event"] == "EDITED"
    assert fake_moderation.events[0]["product_id"] == product.id
    assert db_session.query(ModerationOutboxEvent).one().status == "SENT"


def test_edit_blocked_product_returns_to_on_moderation(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session, status="BLOCKED")
    fake_moderation = override_moderation(client)

    response = client.put(
        f"/api/v1/products/{product.id}",
        json=product_update_payload(),
        headers=auth_headers,
    )

    db_session.refresh(product)
    assert response.status_code == 200
    assert product.status == "ON_MODERATION"
    assert response.json()["status"] == "ON_MODERATION"
    assert len(fake_moderation.events) == 1
    assert fake_moderation.events[0]["event"] == "EDITED"


def test_reserves_preserved_after_sku_edit(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session, status="MODERATED")
    sku = create_sku_fixture(db_session, product, reserved_quantity=7)
    fake_moderation = override_moderation(client)

    response = client.put(
        f"/api/v1/skus/{sku.id}",
        json=sku_update_payload(),
        headers=auth_headers,
    )

    db_session.refresh(sku)
    db_session.refresh(product)
    assert response.status_code == 200
    assert response.json()["reserved_quantity"] == 7
    assert sku.reserved_quantity == 7
    assert product.status == "ON_MODERATION"
    assert len(fake_moderation.events) == 1
    assert fake_moderation.events[0]["event"] == "EDITED"


def test_edit_hard_blocked_returns_403(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session, status="HARD_BLOCKED")
    sku = create_sku_fixture(db_session, product)
    override_moderation(client)

    product_response = client.put(
        f"/api/v1/products/{product.id}",
        json=product_update_payload(),
        headers=auth_headers,
    )
    sku_response = client.put(
        f"/api/v1/skus/{sku.id}",
        json=sku_update_payload(),
        headers=auth_headers,
    )

    assert product_response.status_code == 403
    assert product_response.json() == {
        "code": "FORBIDDEN",
        "message": "Cannot edit hard-blocked product",
    }
    assert sku_response.status_code == 403
    assert sku_response.json() == {
        "code": "FORBIDDEN",
        "message": "Cannot edit hard-blocked product",
    }


def test_edit_others_product_returns_403(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(
        db_session,
        status="MODERATED",
        seller_id=OTHER_SELLER_ID,
    )
    override_moderation(client)
    auth_headers = {
        "Authorization": f"Bearer {create_access_token({'seller_id': TEST_SELLER_ID})}"
    }

    response = client.put(
        f"/api/v1/products/{product.id}",
        json=product_update_payload(),
        headers=auth_headers,
    )

    assert response.status_code == 403
    assert response.json() == {
        "code": "NOT_OWNER",
        "message": "Product does not belong to the authenticated seller",
    }

