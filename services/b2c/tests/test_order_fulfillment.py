from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.b2b_client import B2BUnavailableError
from app.models import FulfillmentRetry, Order, OrderItem
from app.order_fulfillment import (
    process_fulfillment_retries,
    transition_order_to_delivered,
)


USER_ID = "f3d4e5f6-a7b8-4012-8def-123456789012"
SKU_ID = "660e8400-e29b-41d4-a716-446655440001"
PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"


def create_order(db_session: Session, status: str = "DELIVERING") -> Order:
    sequence = create_order.counter
    create_order.counter += 1
    order = Order(
        user_id=USER_ID,
        idempotency_key=f"55555555-6666-4777-8888-{sequence:012d}",
        request_hash=f"{sequence:064x}",
        status=status,
        delivery_address="Ekaterinburg, Mira 19",
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
        self.fulfill_calls: list[dict[str, Any]] = []
        self.processed_orders: set[str] = set()
        self.deduction_count = 0

    def fulfill(
        self,
        *,
        order_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.fulfill_calls.append({"order_id": order_id, "items": items})
        if self.error is not None:
            raise self.error
        if order_id not in self.processed_orders:
            self.processed_orders.add(order_id)
            self.deduction_count += 1
        return {
            "order_id": order_id,
            "status": "FULFILLED",
            "processed_at": "2026-06-09T10:00:00Z",
        }


def test_delivered_status_triggers_fulfill_to_b2b(
    db_session: Session,
) -> None:
    order = create_order(db_session)
    fake_b2b = FakeB2BClient()
    delivered_at = datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc)

    transition_order_to_delivered(
        db_session,
        fake_b2b,
        order,
        delivered_at=delivered_at,
    )

    db_session.refresh(order)
    assert order.status == "DELIVERED"
    assert order.delivered_at == delivered_at.replace(tzinfo=None)
    assert fake_b2b.fulfill_calls == [
        {
            "order_id": order.id,
            "items": [{"sku_id": SKU_ID, "quantity": 2}],
        }
    ]
    assert db_session.query(FulfillmentRetry).count() == 0


def test_fulfill_failure_retried_asynchronously(
    db_session: Session,
) -> None:
    order = create_order(db_session)
    unavailable_b2b = FakeB2BClient(B2BUnavailableError())

    transition_order_to_delivered(db_session, unavailable_b2b, order)

    db_session.refresh(order)
    retry = db_session.query(FulfillmentRetry).one()
    assert order.status == "DELIVERED"
    assert retry.order_id == order.id
    assert retry.attempt_count == 0

    recovered_b2b = FakeB2BClient()
    completed = process_fulfillment_retries(
        db_session,
        recovered_b2b,
        now=datetime.now(timezone.utc),
    )

    db_session.refresh(order)
    assert completed == 1
    assert order.status == "DELIVERED"
    assert db_session.query(FulfillmentRetry).count() == 0
    assert len(recovered_b2b.fulfill_calls) == 1


def test_repeated_fulfill_idempotent(
    db_session: Session,
) -> None:
    order = create_order(db_session)
    fake_b2b = FakeB2BClient()

    first_result = transition_order_to_delivered(db_session, fake_b2b, order)
    second_result = transition_order_to_delivered(db_session, fake_b2b, order)

    assert first_result.id == second_result.id
    assert len(fake_b2b.fulfill_calls) == 2
    assert fake_b2b.fulfill_calls[0] == fake_b2b.fulfill_calls[1]
    assert fake_b2b.deduction_count == 1
