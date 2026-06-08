from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy.orm import Session

from app.b2b_client import B2BClient, B2BResponseError, B2BUnavailableError
from app.models import FulfillmentRetry, Order, OrderItem


logger = logging.getLogger(__name__)
MAX_BACKOFF_SECONDS = 3600


def fulfillment_items(db: Session, order_id: str) -> list[dict[str, object]]:
    return [
        {"sku_id": item.sku_id, "quantity": item.quantity}
        for item in db.query(OrderItem)
        .filter(OrderItem.order_id == order_id)
        .order_by(OrderItem.id)
        .all()
    ]


def _schedule_fulfillment_retry(
    db: Session,
    order: Order,
    error: Exception,
) -> FulfillmentRetry:
    retry = (
        db.query(FulfillmentRetry)
        .filter(FulfillmentRetry.order_id == order.id)
        .one_or_none()
    )
    if retry is None:
        retry = FulfillmentRetry(
            order_id=order.id,
            attempt_count=0,
            next_attempt_at=datetime.now(timezone.utc),
        )
        db.add(retry)
    retry.last_error = str(error) or error.__class__.__name__
    db.commit()
    db.refresh(retry)
    logger.warning("Scheduled fulfillment retry for order %s", order.id)
    return retry


def transition_order_to_delivered(
    db: Session,
    b2b_client: B2BClient,
    order: Order,
    *,
    delivered_at: datetime | None = None,
) -> Order:
    if order.status != "DELIVERED":
        order.status = "DELIVERED"
        order.delivered_at = delivered_at or datetime.now(timezone.utc)
        db.commit()
        db.refresh(order)

    try:
        b2b_client.fulfill(
            order_id=order.id,
            items=fulfillment_items(db, order.id),
        )
    except (B2BUnavailableError, B2BResponseError) as exc:
        _schedule_fulfillment_retry(db, order, exc)
        return order

    retry = (
        db.query(FulfillmentRetry)
        .filter(FulfillmentRetry.order_id == order.id)
        .one_or_none()
    )
    if retry is not None:
        db.delete(retry)
        db.commit()
    return order


def process_fulfillment_retries(
    db: Session,
    b2b_client: B2BClient,
    *,
    now: datetime | None = None,
) -> int:
    current_time = now or datetime.now(timezone.utc)
    retries = (
        db.query(FulfillmentRetry)
        .filter(FulfillmentRetry.next_attempt_at <= current_time)
        .order_by(FulfillmentRetry.next_attempt_at, FulfillmentRetry.id)
        .all()
    )
    completed = 0
    for retry in retries:
        order = db.get(Order, retry.order_id)
        if order is None or order.status != "DELIVERED":
            db.delete(retry)
            db.commit()
            continue
        try:
            b2b_client.fulfill(
                order_id=order.id,
                items=fulfillment_items(db, order.id),
            )
        except (B2BUnavailableError, B2BResponseError) as exc:
            retry.attempt_count += 1
            delay = min(2 ** retry.attempt_count * 60, MAX_BACKOFF_SECONDS)
            retry.next_attempt_at = current_time + timedelta(seconds=delay)
            retry.last_error = str(exc) or exc.__class__.__name__
            db.commit()
            continue

        db.delete(retry)
        db.commit()
        completed += 1
    return completed
