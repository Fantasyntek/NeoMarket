from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.b2b_client import get_b2b_client
from app.models import CartItem, Order, OrderItem, ProcessedB2BEvent


USER_ID = "f3d4e5f6-a7b8-4012-8def-123456789012"
SESSION_ID = "93d4e5f6-a7b8-4012-8def-123456789077"
PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"
SKU_ID = "660e8400-e29b-41d4-a716-446655440001"
SECOND_SKU_ID = "660e8400-e29b-41d4-a716-446655440002"
EVENT_KEY = "44444444-5555-4666-8777-888888888888"
SERVICE_HEADERS = {"X-Service-Key": "dev-service-key"}


class FakeB2BClient:
    def batch_products(self, product_ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "id": PRODUCT_ID,
                "title": "iPhone 15 Pro Max",
                "status": "MODERATED",
                "deleted": False,
                "skus": [
                    {
                        "id": SKU_ID,
                        "product_id": PRODUCT_ID,
                        "name": "256GB Black",
                        "price": 12999000,
                        "discount": 0,
                        "active_quantity": 5,
                        "image": "/s3/iphone.jpg",
                    }
                ],
            }
        ]

    def batch_skus(self, sku_ids: list[str]) -> list[dict[str, Any]]:
        return []


def override_b2b(client: TestClient) -> None:
    client.app.dependency_overrides[get_b2b_client] = lambda: FakeB2BClient()


def auth_headers() -> dict[str, str]:
    token = create_access_token({"sub": USER_ID})
    return {"Authorization": f"Bearer {token}"}


def create_cart_item(
    db_session: Session,
    *,
    sku_id: str = SKU_ID,
    user_id: str | None = USER_ID,
    session_id: str | None = None,
) -> CartItem:
    item = CartItem(
        user_id=user_id,
        session_id=session_id,
        product_id=PRODUCT_ID,
        sku_id=sku_id,
        quantity=2,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def create_order(
    db_session: Session,
    *,
    status: str = "PAID",
) -> Order:
    order = Order(
        user_id=USER_ID,
        idempotency_key="99999999-8888-4777-8666-555555555555",
        request_hash="a" * 64,
        status=status,
        address_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        payment_method_id="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
        total_amount=24998000,
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.id,
            sku_id=SKU_ID,
            product_id=PRODUCT_ID,
            product_title="iPhone 15 Pro Max",
            sku_name="256GB Black",
            quantity=2,
            unit_price=12499000,
            line_total=24998000,
            image_url="/s3/iphone.jpg",
        )
    )
    db_session.commit()
    db_session.refresh(order)
    return order


def product_blocked_payload(
    *,
    key: str = EVENT_KEY,
    sku_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "event": "PRODUCT_BLOCKED",
        "product_id": PRODUCT_ID,
        "sku_ids": sku_ids if sku_ids is not None else [SKU_ID],
        "reason": "moderation rejected product",
        "date": "2026-06-09T10:00:00Z",
    }


def test_product_blocked_marks_cart_items_unavailable(
    client: TestClient,
    db_session: Session,
) -> None:
    cart_item = create_cart_item(db_session)
    create_cart_item(db_session, sku_id=SECOND_SKU_ID)
    override_b2b(client)

    response = client.post(
        "/api/v1/events/product",
        json=product_blocked_payload(),
        headers=SERVICE_HEADERS,
    )
    cart_response = client.get("/api/v1/cart", headers=auth_headers())

    db_session.refresh(cart_item)
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert response.json()["affected_cart_items"] == 1
    assert cart_item.unavailable_reason == "PRODUCT_BLOCKED"
    response_item = next(
        item
        for item in cart_response.json()["items"]
        if item["sku_id"] == SKU_ID
    )
    assert response_item["is_available"] is False
    assert response_item["unavailable_reason"] == "PRODUCT_BLOCKED"


def test_orders_not_affected_by_product_blocked(
    client: TestClient,
    db_session: Session,
) -> None:
    create_cart_item(db_session)
    order = create_order(db_session)

    response = client.post(
        "/api/v1/events/product",
        json=product_blocked_payload(),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(order)
    order_item = db_session.query(OrderItem).filter(OrderItem.order_id == order.id).one()
    assert response.status_code == 200
    assert order.status == "PAID"
    assert order.total_amount == 24998000
    assert order_item.unit_price == 12499000


def test_idempotent_event_no_side_effects(
    client: TestClient,
    db_session: Session,
) -> None:
    cart_item = create_cart_item(db_session)

    first_response = client.post(
        "/api/v1/events/product",
        json=product_blocked_payload(),
        headers=SERVICE_HEADERS,
    )
    second_response = client.post(
        "/api/v1/events/product",
        json=product_blocked_payload(),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(cart_item)
    assert first_response.status_code == 200
    assert first_response.json()["affected_cart_items"] == 1
    assert second_response.status_code == 200
    assert second_response.json()["duplicate"] is True
    assert second_response.json()["affected_cart_items"] == 0
    assert cart_item.unavailable_reason == "PRODUCT_BLOCKED"
    assert db_session.query(ProcessedB2BEvent).count() == 1


def test_missing_service_key_returns_401(client: TestClient) -> None:
    response = client.post(
        "/api/v1/events/product",
        json=product_blocked_payload(),
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_product_deleted_and_out_of_stock_set_canonical_reasons(
    client: TestClient,
    db_session: Session,
) -> None:
    deleted_item = create_cart_item(
        db_session,
        sku_id=SKU_ID,
        user_id=None,
        session_id=SESSION_ID,
    )
    stock_item = create_cart_item(db_session, sku_id=SECOND_SKU_ID)

    deleted_response = client.post(
        "/api/v1/events/product",
        json={
            "idempotency_key": "44444444-5555-4666-8777-888888888889",
            "event": "PRODUCT_DELETED",
            "product_id": PRODUCT_ID,
            "sku_ids": [SKU_ID],
            "date": "2026-06-09T10:00:00Z",
        },
        headers=SERVICE_HEADERS,
    )
    stock_response = client.post(
        "/api/v1/events/product",
        json={
            "idempotency_key": "44444444-5555-4666-8777-888888888890",
            "event": "SKU_OUT_OF_STOCK",
            "product_id": PRODUCT_ID,
            "sku_id": SECOND_SKU_ID,
            "date": "2026-06-09T10:00:00Z",
        },
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(deleted_item)
    db_session.refresh(stock_item)
    assert deleted_response.status_code == 200
    assert stock_response.status_code == 200
    assert deleted_item.unavailable_reason == "PRODUCT_DELETED"
    assert stock_item.unavailable_reason == "OUT_OF_STOCK"


def test_openapi_b2b_event_alias_accepts_nested_payload(
    client: TestClient,
    db_session: Session,
) -> None:
    item = create_cart_item(db_session)

    response = client.post(
        "/api/v1/b2b/events",
        json={
            "idempotency_key": EVENT_KEY,
            "event_type": "PRODUCT_BLOCKED",
            "occurred_at": "2026-06-09T10:00:00Z",
            "payload": {
                "product_id": PRODUCT_ID,
                "reason": "moderation rejected product",
            },
        },
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(item)
    assert response.status_code == 202
    assert item.unavailable_reason == "PRODUCT_BLOCKED"
