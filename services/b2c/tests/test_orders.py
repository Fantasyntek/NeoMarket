from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.b2b_client import (
    B2BResponseError,
    B2BUnavailableError,
    get_b2b_client,
)
from app.models import Order, OrderItem


SKU_ID = "660e8400-e29b-41d4-a716-446655440001"
SECOND_SKU_ID = "660e8400-e29b-41d4-a716-446655440002"
PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"
IDEMPOTENCY_KEY = "11111111-2222-3333-4444-555555555555"
ADDRESS_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PAYMENT_METHOD_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"


def sku_lookup(
    *,
    sku_id: str = SKU_ID,
    name: str = "256GB Black",
    price: int = 12999000,
    discount: int = 500000,
    active_quantity: int = 5,
) -> dict[str, Any]:
    return {
        "product": {
            "id": PRODUCT_ID,
            "title": "iPhone 15 Pro Max",
            "status": "MODERATED",
            "deleted": False,
        },
        "sku": {
            "id": sku_id,
            "product_id": PRODUCT_ID,
            "name": name,
            "price": price,
            "discount": discount,
            "active_quantity": active_quantity,
            "image": "/s3/iphone.jpg",
        },
    }


class FakeB2BClient:
    def __init__(
        self,
        lookups: list[dict[str, Any]] | None = None,
        *,
        reserve_error: Exception | None = None,
        batch_error: Exception | None = None,
    ) -> None:
        self.lookups = lookups if lookups is not None else [sku_lookup()]
        self.reserve_error = reserve_error
        self.batch_error = batch_error
        self.batch_calls: list[list[str]] = []
        self.reserve_calls: list[dict[str, Any]] = []

    def batch_skus(self, sku_ids: list[str]) -> list[dict[str, Any]]:
        self.batch_calls.append(sku_ids)
        if self.batch_error is not None:
            raise self.batch_error
        requested = set(sku_ids)
        return [
            lookup
            for lookup in self.lookups
            if lookup["sku"]["id"] in requested
        ]

    def reserve(
        self,
        *,
        idempotency_key: str,
        order_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.reserve_calls.append(
            {
                "idempotency_key": idempotency_key,
                "order_id": order_id,
                "items": items,
            }
        )
        if self.reserve_error is not None:
            raise self.reserve_error
        return {
            "order_id": order_id,
            "status": "RESERVED",
            "reserved_at": "2026-06-08T10:00:00Z",
        }


def override_b2b(client: TestClient, fake_b2b: FakeB2BClient) -> FakeB2BClient:
    client.app.dependency_overrides[get_b2b_client] = lambda: fake_b2b
    return fake_b2b


def checkout_payload(
    *,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "items": items or [{"sku_id": SKU_ID, "quantity": 2}],
        "address_id": ADDRESS_ID,
        "payment_method_id": PAYMENT_METHOD_ID,
    }


def checkout_headers(
    auth_headers: dict[str, str],
    idempotency_key: str = IDEMPOTENCY_KEY,
) -> dict[str, str]:
    return {**auth_headers, "Idempotency-Key": idempotency_key}


def test_checkout_creates_paid_order_with_fixed_prices(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    fake_b2b = override_b2b(client, FakeB2BClient())

    response = client.post(
        "/api/v1/orders",
        json=checkout_payload(),
        headers=checkout_headers(auth_headers),
    )

    assert response.status_code == 201
    assert response.json()["status"] == "PAID"
    assert response.json()["address"]["id"] == ADDRESS_ID
    assert response.json()["address"]["city"] == "mock"
    assert response.json()["payment_method"]["id"] == PAYMENT_METHOD_ID
    assert "delivery_address" not in response.json()
    assert response.json()["total_amount"] == 24998000
    assert response.json()["items"][0]["unit_price"] == 12499000
    assert response.json()["items"][0]["product_title"] == "iPhone 15 Pro Max"
    assert response.json()["items"][0]["sku_name"] == "256GB Black"
    order_item = db_session.query(OrderItem).one()
    assert order_item.unit_price == 12499000
    assert order_item.line_total == 24998000
    assert fake_b2b.reserve_calls[0]["items"] == [
        {"sku_id": SKU_ID, "quantity": 2}
    ]

    fake_b2b.lookups[0]["sku"]["price"] = 19999000
    assert db_session.query(OrderItem).one().unit_price == 12499000


def test_partial_reserve_failure_returns_409(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    failed_items = [
        {
            "sku_id": SECOND_SKU_ID,
            "requested": 3,
            "available": 1,
            "reason": "INSUFFICIENT_STOCK",
        }
    ]
    fake_b2b = override_b2b(
        client,
        FakeB2BClient(
            [
                sku_lookup(),
                sku_lookup(
                    sku_id=SECOND_SKU_ID,
                    name="512GB Blue",
                    active_quantity=3,
                ),
            ],
            reserve_error=B2BResponseError(
                409,
                {"reserved": False, "failed_items": failed_items},
            ),
        ),
    )

    response = client.post(
        "/api/v1/orders",
        json=checkout_payload(
            items=[
                {"sku_id": SKU_ID, "quantity": 1},
                {"sku_id": SECOND_SKU_ID, "quantity": 3},
            ]
        ),
        headers=checkout_headers(auth_headers),
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "RESERVE_FAILED",
        "message": "Failed to reserve order items",
        "failed_items": failed_items,
    }
    assert len(fake_b2b.reserve_calls) == 1
    assert db_session.query(Order).count() == 0
    assert db_session.query(OrderItem).count() == 0


def test_idempotency_returns_existing_order(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    fake_b2b = override_b2b(client, FakeB2BClient())

    first_response = client.post(
        "/api/v1/orders",
        json=checkout_payload(),
        headers=checkout_headers(auth_headers),
    )
    second_response = client.post(
        "/api/v1/orders",
        json=checkout_payload(),
        headers=checkout_headers(auth_headers),
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 200
    assert second_response.json()["id"] == first_response.json()["id"]
    assert second_response.json()["items"] == first_response.json()["items"]
    assert len(fake_b2b.reserve_calls) == 1
    assert db_session.query(Order).count() == 1
    assert db_session.query(OrderItem).count() == 1


def test_b2b_unavailable_returns_503(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(
        client,
        FakeB2BClient(batch_error=B2BUnavailableError()),
    )

    response = client.post(
        "/api/v1/orders",
        json=checkout_payload(),
        headers=checkout_headers(auth_headers),
    )

    assert response.status_code == 503
    assert response.json()["code"] == "B2B_UNAVAILABLE"
    assert db_session.query(Order).count() == 0
    assert db_session.query(OrderItem).count() == 0


def test_insufficient_stock_is_rejected_before_reserve(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    fake_b2b = override_b2b(
        client,
        FakeB2BClient([sku_lookup(active_quantity=1)]),
    )

    response = client.post(
        "/api/v1/orders",
        json=checkout_payload(),
        headers=checkout_headers(auth_headers),
    )

    assert response.status_code == 409
    assert response.json()["failed_items"][0]["reason"] == "INSUFFICIENT_STOCK"
    assert fake_b2b.reserve_calls == []
    assert db_session.query(Order).count() == 0


def test_openapi_idempotency_header_and_items_snapshot_are_supported(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(client, FakeB2BClient())
    headers = checkout_headers(auth_headers)

    response = client.post(
        "/api/v1/orders",
        json={
            "address_id": ADDRESS_ID,
            "payment_method_id": PAYMENT_METHOD_ID,
            "items_snapshot": [
                {
                    "sku_id": SKU_ID,
                    "quantity": 1,
                    "unit_price": 1,
                }
            ],
        },
        headers=headers,
    )

    assert response.status_code == 201
    assert response.json()["items"][0]["unit_price"] == 12499000


def test_checkout_requires_openapi_idempotency_header(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(client, FakeB2BClient())

    response = client.post(
        "/api/v1/orders",
        json=checkout_payload(),
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "Idempotency-Key is required",
    }


def test_checkout_requires_address_and_payment_method_ids(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(client, FakeB2BClient())

    response = client.post(
        "/api/v1/orders",
        json={"items": [{"sku_id": SKU_ID, "quantity": 1}]},
        headers=checkout_headers(auth_headers),
    )

    assert response.status_code == 400
    assert response.json()["message"] == "address_id is required"


def test_idempotency_key_cannot_be_reused_by_another_user(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    fake_b2b = override_b2b(client, FakeB2BClient())
    first_response = client.post(
        "/api/v1/orders",
        json=checkout_payload(),
        headers=checkout_headers(auth_headers),
    )
    other_token = create_access_token(
        {"sub": "f3d4e5f6-a7b8-4012-8def-123456789099"}
    )
    other_headers = checkout_headers(
        {"Authorization": f"Bearer {other_token}"}
    )

    second_response = client.post(
        "/api/v1/orders",
        json=checkout_payload(),
        headers=other_headers,
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 409
    assert second_response.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(fake_b2b.reserve_calls) == 1
