from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.b2b_client import B2BResponseError, B2BUnavailableError, get_b2b_client
from app.cancellation_retry import process_cancellation_retries
from app.models import CancellationRetry, Order, OrderItem


USER_ID = "f3d4e5f6-a7b8-4012-8def-123456789012"
OTHER_USER_ID = "a3d4e5f6-a7b8-4012-8def-123456789099"
SKU_ID = "660e8400-e29b-41d4-a716-446655440001"
PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"
ADDRESS_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PAYMENT_METHOD_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"


def auth_headers(user_id: str = USER_ID) -> dict[str, str]:
    token = create_access_token({"sub": user_id})
    return {"Authorization": f"Bearer {token}"}


def create_order(
    db_session: Session,
    *,
    user_id: str = USER_ID,
    status: str = "PAID",
) -> Order:
    sequence = create_order.counter
    create_order.counter += 1
    order = Order(
        user_id=user_id,
        idempotency_key=f"22222222-3333-4444-8555-{sequence:012d}",
        request_hash=f"{sequence:064x}",
        status=status,
        address_id=ADDRESS_ID,
        payment_method_id=PAYMENT_METHOD_ID,
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


create_order.counter = 1


class FakeB2BClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.unreserve_calls: list[dict[str, Any]] = []

    def unreserve(
        self,
        *,
        order_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.unreserve_calls.append({"order_id": order_id, "items": items})
        if self.error is not None:
            raise self.error
        return {
            "order_id": order_id,
            "status": "UNRESERVED",
            "processed_at": "2026-06-08T10:00:00Z",
        }


def override_b2b(client: TestClient, fake: FakeB2BClient) -> FakeB2BClient:
    client.app.dependency_overrides[get_b2b_client] = lambda: fake
    return fake


def test_cancel_paid_order_transitions_to_cancelled(
    client: TestClient,
    db_session: Session,
) -> None:
    order = create_order(db_session)
    fake = override_b2b(client, FakeB2BClient())

    response = client.post(
        f"/api/v1/orders/{order.id}/cancel",
        headers=auth_headers(),
    )

    db_session.refresh(order)
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["address"]["id"] == ADDRESS_ID
    assert response.json()["address"]["city"] == "mock"
    assert response.json()["payment_method"]["id"] == PAYMENT_METHOD_ID
    assert response.json()["buyer_id"] == USER_ID
    assert response.json()["subtotal"] == 24998000
    assert response.json()["total"] == 24998000
    assert "delivery_address" not in response.json()
    assert order.status == "CANCELLED"
    assert fake.unreserve_calls == [
        {
            "order_id": order.id,
            "items": [{"sku_id": SKU_ID, "quantity": 2}],
        }
    ]
    assert db_session.query(CancellationRetry).count() == 0


def test_unreserve_failure_transitions_to_cancel_pending(
    client: TestClient,
    db_session: Session,
) -> None:
    order = create_order(db_session)
    override_b2b(client, FakeB2BClient(B2BUnavailableError()))

    response = client.post(
        f"/api/v1/orders/{order.id}/cancel",
        headers=auth_headers(),
    )

    db_session.refresh(order)
    retry = db_session.query(CancellationRetry).one()
    assert response.status_code == 200
    assert response.json()["status"] == "CANCEL_PENDING"
    assert response.json()["address"]["id"] == ADDRESS_ID
    assert response.json()["payment_method"]["id"] == PAYMENT_METHOD_ID
    assert order.status == "CANCEL_PENDING"
    assert retry.order_id == order.id
    assert retry.attempt_count == 0


def test_cancel_assembling_order_returns_409(
    client: TestClient,
    db_session: Session,
) -> None:
    order = create_order(db_session, status="ASSEMBLING")
    fake = override_b2b(client, FakeB2BClient())

    response = client.post(
        f"/api/v1/orders/{order.id}/cancel",
        headers=auth_headers(),
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "CANCEL_NOT_ALLOWED",
        "message": "Cancellation is not allowed for status ASSEMBLING",
        "current_status": "ASSEMBLING",
    }
    assert fake.unreserve_calls == []


def test_other_user_order_returns_404(
    client: TestClient,
    db_session: Session,
) -> None:
    order = create_order(db_session, user_id=OTHER_USER_ID)
    fake = override_b2b(client, FakeB2BClient())

    response = client.post(
        f"/api/v1/orders/{order.id}/cancel",
        headers=auth_headers(USER_ID),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "ORDER_NOT_FOUND"
    assert fake.unreserve_calls == []


def test_unreserve_non_retryable_error_is_propagated(
    client: TestClient,
    db_session: Session,
) -> None:
    order = create_order(db_session)
    override_b2b(
        client,
        FakeB2BClient(
            B2BResponseError(
                409,
                {
                    "code": "UNRESERVE_CONFLICT",
                    "message": "Reservation cannot be released",
                },
            )
        ),
    )

    response = client.post(
        f"/api/v1/orders/{order.id}/cancel",
        headers=auth_headers(),
    )

    db_session.refresh(order)
    assert response.status_code == 409
    assert response.json() == {
        "code": "UNRESERVE_CONFLICT",
        "message": "Reservation cannot be released",
    }
    assert order.status == "PAID"
    assert db_session.query(CancellationRetry).count() == 0


def test_pending_cancellation_retry_completes_after_b2b_recovers(
    client: TestClient,
    db_session: Session,
) -> None:
    order = create_order(db_session)
    override_b2b(client, FakeB2BClient(B2BUnavailableError()))
    client.post(
        f"/api/v1/orders/{order.id}/cancel",
        headers=auth_headers(),
    )
    recovered_b2b = FakeB2BClient()

    completed = process_cancellation_retries(
        db_session,
        recovered_b2b,
        now=datetime.now(timezone.utc),
    )

    db_session.refresh(order)
    assert completed == 1
    assert order.status == "CANCELLED"
    assert db_session.query(CancellationRetry).count() == 0
    assert len(recovered_b2b.unreserve_calls) == 1
