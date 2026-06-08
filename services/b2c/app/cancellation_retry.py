from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy.orm import Session

from app.b2b_client import B2BClient, B2BResponseError, B2BUnavailableError
from app.models import CancellationRetry, Order, OrderItem


logger = logging.getLogger(__name__)
MAX_BACKOFF_SECONDS = 3600


def cancellation_items(db: Session, order_id: str) -> list[dict[str, object]]:
    return [
        {"sku_id": item.sku_id, "quantity": item.quantity}
        for item in db.query(OrderItem)
        .filter(OrderItem.order_id == order_id)
        .order_by(OrderItem.id)
        .all()
    ]


def schedule_cancellation_retry(
    db: Session,
    order: Order,
    error: Exception,
) -> CancellationRetry:
    retry = (
        db.query(CancellationRetry)
        .filter(CancellationRetry.order_id == order.id)
        .one_or_none()
    )
    if retry is None:
        retry = CancellationRetry(
            order_id=order.id,
            attempt_count=0,
            next_attempt_at=datetime.now(timezone.utc),
        )
        db.add(retry)
    retry.last_error = str(error) or error.__class__.__name__
    order.status = "CANCEL_PENDING"
    db.commit()
    db.refresh(retry)
    logger.warning("Scheduled cancellation retry for order %s", order.id)
    return retry


def process_cancellation_retries(
    db: Session,
    b2b_client: B2BClient,
    *,
    now: datetime | None = None,
) -> int:
    current_time = now or datetime.now(timezone.utc)
    retries = (
        db.query(CancellationRetry)
        .filter(CancellationRetry.next_attempt_at <= current_time)
        .order_by(CancellationRetry.next_attempt_at, CancellationRetry.id)
        .all()
    )
    completed = 0
    for retry in retries:
        order = db.get(Order, retry.order_id)
        if order is None or order.status != "CANCEL_PENDING":
            db.delete(retry)
            db.commit()
            continue
        try:
            b2b_client.unreserve(
                order_id=order.id,
                items=cancellation_items(db, order.id),
            )
        except (B2BUnavailableError, B2BResponseError) as exc:
            retry.attempt_count += 1
            delay = min(2 ** retry.attempt_count * 60, MAX_BACKOFF_SECONDS)
            retry.next_attempt_at = current_time + timedelta(seconds=delay)
            retry.last_error = str(exc) or exc.__class__.__name__
            db.commit()
            continue

        order.status = "CANCELLED"
        db.delete(retry)
        db.commit()
        completed += 1
    return completed
