from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.b2b_client import get_b2b_client
from app.models import Order, OrderItem


USER_ID = "f3d4e5f6-a7b8-4012-8def-123456789012"
OTHER_USER_ID = "a3d4e5f6-a7b8-4012-8def-123456789099"
SKU_ID = "660e8400-e29b-41d4-a716-446655440001"
PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"
ADDRESS_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PAYMENT_METHOD_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"


def headers_for(user_id: str) -> dict[str, str]:
    token = create_access_token({"sub": user_id})
    return {"Authorization": f"Bearer {token}"}


def create_order(
    db_session: Session,
    *,
    user_id: str,
    status: str = "PAID",
    unit_price: int = 12999000,
    quantity: int = 1,
    created_at: datetime | None = None,
) -> Order:
    order = Order(
        user_id=user_id,
        idempotency_key=f"11111111-2222-4333-8444-{create_order.counter:012d}",
        request_hash=f"{create_order.counter:064x}",
        status=status,
        address_id=ADDRESS_ID,
        payment_method_id=PAYMENT_METHOD_ID,
        total_amount=unit_price * quantity,
        created_at=created_at or datetime.now(timezone.utc),
        updated_at=created_at or datetime.now(timezone.utc),
    )
    create_order.counter += 1
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.id,
            sku_id=SKU_ID,
            product_id=PRODUCT_ID,
            product_title="iPhone 15 Pro Max",
            sku_name="256GB Black",
            quantity=quantity,
            unit_price=unit_price,
            line_total=unit_price * quantity,
            image_url="/s3/iphone.jpg",
        )
    )
    db_session.commit()
    db_session.refresh(order)
    return order


create_order.counter = 1


def test_orders_list_returns_own_orders_paginated(
    client: TestClient,
    db_session: Session,
) -> None:
    now = datetime.now(timezone.utc)
    older = create_order(
        db_session,
        user_id=USER_ID,
        status="DELIVERED",
        created_at=now - timedelta(days=2),
    )
    newer = create_order(
        db_session,
        user_id=USER_ID,
        status="PAID",
        quantity=2,
        created_at=now - timedelta(days=1),
    )
    create_order(
        db_session,
        user_id=OTHER_USER_ID,
        created_at=now,
    )

    first_page = client.get(
        "/api/v1/orders?limit=1&offset=0",
        headers=headers_for(USER_ID),
    )
    second_page = client.get(
        "/api/v1/orders?limit=1&offset=1",
        headers=headers_for(USER_ID),
    )

    assert first_page.status_code == 200
    first_page_payload = first_page.json()
    assert first_page_payload["total_count"] == 2
    assert first_page_payload["limit"] == 1
    assert first_page_payload["offset"] == 0
    assert len(first_page_payload["items"]) == 1
    listed_order = first_page_payload["items"][0]
    assert listed_order["id"] == newer.id
    assert listed_order["buyer_id"] == USER_ID
    assert listed_order["status"] == "PAID"
    assert listed_order["subtotal"] == 25998000
    assert listed_order["total"] == 25998000
    assert listed_order["address"]["id"] == ADDRESS_ID
    assert listed_order["items"][0]["unit_price"] == 12999000
    assert listed_order["items"][0]["quantity"] == 2
    assert second_page.status_code == 200
    assert second_page.json()["items"][0]["id"] == older.id
    assert second_page.json()["total_count"] == 2


def test_order_detail_shows_fixed_prices(
    client: TestClient,
    db_session: Session,
) -> None:
    order = create_order(
        db_session,
        user_id=USER_ID,
        unit_price=12499000,
        quantity=2,
    )

    class B2BMustNotBeCalled:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"Order detail must not call B2B: {name}")

    client.app.dependency_overrides[get_b2b_client] = lambda: B2BMustNotBeCalled()
    current_sku_price = 19999000

    response = client.get(
        f"/api/v1/orders/{order.id}",
        headers=headers_for(USER_ID),
    )

    assert current_sku_price != 12499000
    assert response.status_code == 200
    assert response.json()["items"][0]["unit_price"] == 12499000
    assert response.json()["items"][0]["line_total"] == 24998000
    assert response.json()["total_amount"] == 24998000
    assert response.json()["buyer_id"] == USER_ID
    assert response.json()["subtotal"] == 24998000
    assert response.json()["total"] == 24998000
    assert response.json()["address"]["id"] == ADDRESS_ID
    assert response.json()["payment_method"]["id"] == PAYMENT_METHOD_ID


def test_other_user_order_returns_404_not_403(
    client: TestClient,
    db_session: Session,
) -> None:
    order = create_order(db_session, user_id=OTHER_USER_ID)

    response = client.get(
        f"/api/v1/orders/{order.id}",
        headers=headers_for(USER_ID),
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "ORDER_NOT_FOUND",
        "message": "Order not found",
    }


def test_orders_list_status_filter_works(
    client: TestClient,
    db_session: Session,
) -> None:
    create_order(db_session, user_id=USER_ID, status="PAID")
    delivered = create_order(db_session, user_id=USER_ID, status="DELIVERED")

    response = client.get(
        "/api/v1/orders?status=DELIVERED",
        headers=headers_for(USER_ID),
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [delivered.id]
    assert response.json()["total_count"] == 1


def test_orders_list_ignores_user_id_query_parameter(
    client: TestClient,
    db_session: Session,
) -> None:
    own_order = create_order(db_session, user_id=USER_ID)
    create_order(db_session, user_id=OTHER_USER_ID)

    response = client.get(
        f"/api/v1/orders?user_id={OTHER_USER_ID}",
        headers=headers_for(USER_ID),
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [own_order.id]


def test_nonexistent_and_malformed_order_return_same_404(
    client: TestClient,
) -> None:
    missing = client.get(
        "/api/v1/orders/99999999-8888-4777-8666-555555555555",
        headers=headers_for(USER_ID),
    )
    malformed = client.get(
        "/api/v1/orders/not-a-uuid",
        headers=headers_for(USER_ID),
    )

    assert missing.status_code == 404
    assert malformed.status_code == 404
    assert missing.json() == malformed.json()
