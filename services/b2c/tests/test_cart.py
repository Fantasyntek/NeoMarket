from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.b2b_client import get_b2b_client
from app.models import CartItem


USER_ID = "f3d4e5f6-a7b8-4012-8def-123456789012"
OTHER_USER_ID = "a3d4e5f6-a7b8-4012-8def-123456789099"
SESSION_ID = "93d4e5f6-a7b8-4012-8def-123456789077"
SKU_ID = "660e8400-e29b-41d4-a716-446655440001"
PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"


def sku_lookup(
    active_quantity: int = 10,
    price: int = 12999000,
    status: str = "MODERATED",
    deleted: bool = False,
) -> dict[str, Any]:
    return {
        "product": {
            "id": PRODUCT_ID,
            "title": "iPhone 15 Pro Max",
            "status": status,
            "deleted": deleted,
        },
        "sku": {
            "id": SKU_ID,
            "product_id": PRODUCT_ID,
            "name": "256GB Black",
            "price": price,
            "discount": 500000,
            "active_quantity": active_quantity,
            "image": "/s3/iphone-black.jpg",
            "images": [
                {
                    "id": SKU_ID,
                    "url": "/s3/iphone-black.jpg",
                    "ordering": 0,
                }
            ],
        },
    }


class FakeB2BClient:
    def __init__(
        self,
        lookups: list[dict[str, Any]] | None = None,
    ) -> None:
        self.lookups = lookups if lookups is not None else [sku_lookup()]
        self.get_sku_calls: list[str] = []
        self.batch_product_calls: list[list[str]] = []
        self.batch_sku_calls: list[list[str]] = []

    def get_sku(self, sku_id: str) -> dict[str, Any]:
        self.get_sku_calls.append(sku_id)
        for lookup in self.lookups:
            if lookup["sku"]["id"] == sku_id:
                return lookup
        raise AssertionError(f"Unexpected SKU lookup: {sku_id}")

    def batch_skus(self, sku_ids: list[str]) -> list[dict[str, Any]]:
        self.batch_sku_calls.append(sku_ids)
        requested = set(sku_ids)
        return [
            lookup
            for lookup in self.lookups
            if lookup["sku"]["id"] in requested
        ]

    def batch_products(self, product_ids: list[str]) -> list[dict[str, Any]]:
        self.batch_product_calls.append(product_ids)
        requested = set(product_ids)
        products: dict[str, dict[str, Any]] = {}
        for lookup in self.lookups:
            product = lookup["product"]
            sku = lookup["sku"]
            if product["id"] not in requested or int(sku["active_quantity"]) <= 0:
                continue
            products.setdefault(product["id"], {**product, "skus": []})["skus"].append(sku)
        return list(products.values())


def override_b2b(client: TestClient, fake_b2b: FakeB2BClient) -> FakeB2BClient:
    client.app.dependency_overrides[get_b2b_client] = lambda: fake_b2b
    return fake_b2b


def auth_headers(
    user_id: str = USER_ID,
    session_id: str | None = None,
) -> dict[str, str]:
    token = create_access_token({"sub": user_id})
    headers = {"Authorization": f"Bearer {token}"}
    if session_id:
        headers["X-Session-Id"] = session_id
    return headers


def guest_headers(session_id: str = SESSION_ID) -> dict[str, str]:
    return {"X-Session-Id": session_id}


def create_cart_item(
    db_session: Session,
    *,
    quantity: int,
    user_id: str | None = None,
    session_id: str | None = None,
    sku_id: str = SKU_ID,
) -> CartItem:
    item = CartItem(
        user_id=user_id,
        session_id=session_id,
        product_id=PRODUCT_ID,
        sku_id=sku_id,
        quantity=quantity,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def test_add_sku_increments_quantity_if_already_in_cart(
    client: TestClient,
    db_session: Session,
) -> None:
    fake_b2b = override_b2b(client, FakeB2BClient())

    first_response = client.post(
        "/api/v1/cart/items",
        json={"sku_id": SKU_ID, "quantity": 1},
        headers=guest_headers(),
    )
    second_response = client.post(
        "/api/v1/cart/items",
        json={"sku_id": SKU_ID, "quantity": 2},
        headers=guest_headers(),
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 200
    assert second_response.json()["items"][0]["quantity"] == 3
    assert db_session.query(CartItem).one().quantity == 3
    assert fake_b2b.get_sku_calls == [SKU_ID, SKU_ID]


def test_get_cart_enriched_with_b2b_data(
    client: TestClient,
    db_session: Session,
) -> None:
    create_cart_item(db_session, quantity=2, user_id=USER_ID)
    fake_b2b = override_b2b(client, FakeB2BClient([sku_lookup(price=1000000)]))

    response = client.get("/api/v1/cart", headers=auth_headers())

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["name"] == "iPhone 15 Pro Max 256GB Black"
    assert item["unit_price"] == 500000
    assert item["line_total"] == 1000000
    assert item["available_quantity"] == 10
    assert response.json()["summary"] == {
        "total_amount": 1000000,
        "total_items": 2,
        "unavailable_count": 0,
        "checkout_ready": True,
    }
    assert response.json()["checkout_payload"]["items"] == [
        {"sku_id": SKU_ID, "quantity": 2, "unit_price": 500000}
    ]
    assert fake_b2b.batch_product_calls == [[PRODUCT_ID]]
    assert fake_b2b.batch_sku_calls == []


def test_unavailable_sku_shown_with_reason(
    client: TestClient,
    db_session: Session,
) -> None:
    create_cart_item(db_session, quantity=2, user_id=USER_ID)
    fake_b2b = override_b2b(
        client,
        FakeB2BClient([sku_lookup(active_quantity=0)]),
    )

    response = client.get("/api/v1/cart", headers=auth_headers())

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["is_available"] is False
    assert item["unavailable_reason"] == "OUT_OF_STOCK"
    assert item["line_total"] == 0
    assert response.json()["subtotal"] == 0
    assert response.json()["summary"]["unavailable_count"] == 1
    assert response.json()["summary"]["checkout_ready"] is False
    assert fake_b2b.batch_product_calls == [[PRODUCT_ID]]
    assert fake_b2b.batch_sku_calls == [[SKU_ID]]


def test_guest_cart_merged_on_login(
    client: TestClient,
    db_session: Session,
) -> None:
    create_cart_item(db_session, quantity=5, session_id=SESSION_ID)
    create_cart_item(db_session, quantity=2, user_id=USER_ID)
    override_b2b(client, FakeB2BClient())

    response = client.get(
        "/api/v1/cart",
        headers=auth_headers(session_id=SESSION_ID),
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["quantity"] == 5
    items = db_session.query(CartItem).all()
    assert len(items) == 1
    assert items[0].user_id == USER_ID
    assert items[0].session_id is None
    assert items[0].quantity == 5


def test_missing_cart_identity_returns_400(client: TestClient) -> None:
    response = client.get("/api/v1/cart")

    assert response.status_code == 400
    assert response.json()["code"] == "MISSING_CART_IDENTITY"


def test_other_users_cart_item_returns_404(
    client: TestClient,
    db_session: Session,
) -> None:
    item = create_cart_item(db_session, quantity=1, user_id=OTHER_USER_ID)
    override_b2b(client, FakeB2BClient())

    response = client.put(
        f"/api/v1/cart/items/{item.id}",
        json={"quantity": 2},
        headers=auth_headers(USER_ID),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_missing_product_lookup_shows_product_deleted(
    client: TestClient,
    db_session: Session,
) -> None:
    create_cart_item(db_session, quantity=1, user_id=USER_ID)
    override_b2b(client, FakeB2BClient([]))

    response = client.get("/api/v1/cart", headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["items"][0]["unavailable_reason"] == "PRODUCT_DELETED"
    assert response.json()["items"][0]["line_total"] == 0


def test_blocked_product_shown_with_blocked_reason(
    client: TestClient,
    db_session: Session,
) -> None:
    create_cart_item(db_session, quantity=1, user_id=USER_ID)
    override_b2b(
        client,
        FakeB2BClient([sku_lookup(status="BLOCKED")]),
    )

    response = client.get("/api/v1/cart", headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["items"][0]["unavailable_reason"] == "PRODUCT_BLOCKED"
    assert response.json()["items"][0]["line_total"] == 0


def test_explicit_merge_endpoint_uses_max_quantity(
    client: TestClient,
    db_session: Session,
) -> None:
    create_cart_item(db_session, quantity=3, session_id=SESSION_ID)
    create_cart_item(db_session, quantity=7, user_id=USER_ID)
    override_b2b(client, FakeB2BClient())

    response = client.post(
        "/api/v1/cart/merge",
        headers=auth_headers(session_id=SESSION_ID),
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["quantity"] == 7


def test_update_delete_and_clear_cart_items(
    client: TestClient,
    db_session: Session,
) -> None:
    item = create_cart_item(db_session, quantity=1, user_id=USER_ID)
    override_b2b(client, FakeB2BClient())

    update_response = client.put(
        f"/api/v1/cart/items/{item.id}",
        json={"quantity": 4},
        headers=auth_headers(),
    )
    delete_response = client.delete(
        f"/api/v1/cart/items/{item.id}",
        headers=auth_headers(),
    )
    clear_response = client.delete("/api/v1/cart", headers=auth_headers())

    assert update_response.status_code == 200
    assert update_response.json()["items"][0]["quantity"] == 4
    assert delete_response.status_code == 200
    assert delete_response.json()["items"] == []
    assert clear_response.status_code == 204
    assert db_session.query(CartItem).count() == 0
