from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.b2b import (
    B2BDispatcher,
    dispatch_b2b_and_mark_sent,
    get_b2b_dispatcher,
)
from app.database import SessionLocal
from app.models import B2BOutboxEvent


def retry_pending_b2b_events(
    db: Session,
    dispatcher: B2BDispatcher,
    *,
    limit: int = 100,
) -> int:
    events = (
        db.query(B2BOutboxEvent)
        .filter(B2BOutboxEvent.status == "PENDING")
        .order_by(B2BOutboxEvent.created_at.asc(), B2BOutboxEvent.id.asc())
        .limit(limit)
        .all()
    )
    sent = 0
    for event in events:
        payload = json.loads(event.payload_json)
        dispatch_b2b_and_mark_sent(db, dispatcher, payload, event)
        db.refresh(event)
        if event.status == "SENT":
            sent += 1
    return sent


def main() -> None:
    with SessionLocal() as db:
        retry_pending_b2b_events(db, get_b2b_dispatcher())


if __name__ == "__main__":
    main()
