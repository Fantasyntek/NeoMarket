from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse, Response
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import CurrentModerator, require_moderator
from app.database import get_db
from app.errors import api_error
from app.models import ProductModeration


router = APIRouter(prefix="/api/v1", tags=["Queue"])


def _claim_ttl() -> timedelta:
    try:
        minutes = int(os.getenv("CLAIM_TTL_MINUTES", "30"))
    except ValueError:
        minutes = 30
    return timedelta(minutes=max(minutes, 1))


def _normalize_request(payload: dict[str, Any] | None) -> tuple[int | None, list[str]]:
    payload = payload or {}
    queue_priority = payload.get(
        "queue_priority",
        payload.get("queueId", payload.get("queue_id")),
    )
    if queue_priority is not None and (
        not isinstance(queue_priority, int)
        or isinstance(queue_priority, bool)
        or not 1 <= queue_priority <= 4
    ):
        raise api_error(
            400,
            "INVALID_QUEUE_PRIORITY",
            "queue_priority must be between 1 and 4",
        )

    category_ids = payload.get("category_ids", [])
    if not isinstance(category_ids, list):
        raise api_error(400, "INVALID_REQUEST", "category_ids must be an array")
    normalized_categories: list[str] = []
    for category_id in category_ids:
        try:
            normalized_categories.append(str(UUID(category_id)))
        except (TypeError, ValueError):
            raise api_error(
                400,
                "INVALID_REQUEST",
                "category_ids must contain valid UUIDs",
            )
    return queue_priority, normalized_categories


def _release_expired_claims(db: Session, now: datetime) -> None:
    db.execute(
        update(ProductModeration)
        .where(
            ProductModeration.status == "IN_REVIEW",
            ProductModeration.claim_expires_at.is_not(None),
            ProductModeration.claim_expires_at <= now,
        )
        .values(
            status="PENDING",
            moderator_id=None,
            claimed_at=None,
            claim_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )


def _moderator_has_active_claim(
    db: Session,
    moderator_id: str,
    now: datetime,
) -> bool:
    return (
        db.query(ProductModeration.id)
        .filter(
            ProductModeration.status == "IN_REVIEW",
            ProductModeration.moderator_id == moderator_id,
            ProductModeration.claim_expires_at > now,
        )
        .first()
        is not None
    )


def _candidate_query(
    db: Session,
    queue_priority: int | None,
    category_ids: list[str],
):
    query = db.query(ProductModeration).filter(
        ProductModeration.status == "PENDING",
        ProductModeration.archived.is_(False),
    )
    if queue_priority is not None:
        query = query.filter(ProductModeration.queue_priority == queue_priority)
    if category_ids:
        query = query.filter(ProductModeration.category_id.in_(category_ids))
    query = query.order_by(
        ProductModeration.queue_priority.asc(),
        ProductModeration.date_created.asc(),
        ProductModeration.id.asc(),
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    return query


def _claim_next(
    db: Session,
    moderator_id: str,
    queue_priority: int | None,
    category_ids: list[str],
) -> ProductModeration | None:
    now = datetime.now(timezone.utc)
    expires_at = now + _claim_ttl()
    _release_expired_claims(db, now)

    if _moderator_has_active_claim(db, moderator_id, now):
        db.rollback()
        raise api_error(
            409,
            "MODERATOR_ALREADY_IN_REVIEW",
            "Moderator already has an active card",
        )

    for _ in range(20):
        candidate = _candidate_query(db, queue_priority, category_ids).first()
        if candidate is None:
            db.commit()
            return None

        result = db.execute(
            update(ProductModeration)
            .where(
                ProductModeration.id == candidate.id,
                ProductModeration.status == "PENDING",
            )
            .values(
                status="IN_REVIEW",
                moderator_id=moderator_id,
                claimed_at=now,
                claim_expires_at=expires_at,
                review_revision=candidate.content_revision,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise api_error(
                    409,
                    "MODERATOR_ALREADY_IN_REVIEW",
                    "Moderator already has an active card",
                )
            return db.get(ProductModeration, candidate.id)
        db.rollback()
    raise api_error(409, "QUEUE_BUSY", "Queue is busy, retry the request")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _serialize(card: ProductModeration) -> dict[str, Any]:
    return {
        "id": card.id,
        "product_id": card.product_id,
        "seller_id": card.seller_id,
        "category_id": card.category_id,
        "kind": card.kind,
        "status": card.status,
        "queue_priority": card.queue_priority,
        "assigned_moderator_id": card.moderator_id,
        "claimed_at": _iso(card.claimed_at),
        "claim_expires_at": _iso(card.claim_expires_at),
        "decision_at": _iso(card.decision_at),
        "created_at": _iso(card.date_created),
        "updated_at": _iso(card.date_updated),
    }


def _handle_claim(
    payload: dict[str, Any] | None,
    moderator: CurrentModerator,
    db: Session,
) -> Response:
    queue_priority, category_ids = _normalize_request(payload)
    card = _claim_next(
        db,
        moderator.moderator_id,
        queue_priority,
        category_ids,
    )
    if card is None:
        return Response(status_code=204)
    return JSONResponse(status_code=200, content=_serialize(card))


@router.post("/queue/claim", response_model=None)
def claim_next_openapi(
    payload: dict[str, Any] | None = Body(default=None),
    moderator: CurrentModerator = Depends(require_moderator),
    db: Session = Depends(get_db),
) -> Response:
    return _handle_claim(payload, moderator, db)


@router.post("/product-moderation/get-next", response_model=None)
def claim_next_canonical(
    payload: dict[str, Any] | None = Body(default=None),
    moderator: CurrentModerator = Depends(require_moderator),
    db: Session = Depends(get_db),
) -> Response:
    return _handle_claim(payload, moderator, db)
