from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import api_error
from app.models import Banner, BannerEvent


router = APIRouter(prefix="/api/v1", tags=["Banners"])
ALLOWED_BANNER_EVENTS = {"impression", "click"}


def _serialize_banner(banner: Banner) -> dict[str, Any]:
    return {
        "id": banner.id,
        "title": banner.title,
        "image_url": banner.image_url,
        "link": banner.link,
        "priority": banner.priority,
        "ordering": banner.priority,
        "start_at": banner.start_at.isoformat() if banner.start_at else None,
        "end_at": banner.end_at.isoformat() if banner.end_at else None,
        "active_from": banner.start_at.isoformat() if banner.start_at else None,
        "active_to": banner.end_at.isoformat() if banner.end_at else None,
    }


def _active_banners(db: Session) -> list[Banner]:
    now = datetime.now(timezone.utc)
    return (
        db.query(Banner)
        .filter(
            Banner.is_active.is_(True),
            or_(Banner.start_at.is_(None), Banner.start_at <= now),
            or_(Banner.end_at.is_(None), Banner.end_at >= now),
        )
        .order_by(Banner.priority, Banner.created_at, Banner.id)
        .all()
    )


@router.get("/home/banners")
def list_home_banners(db: Session = Depends(get_db)) -> dict[str, Any]:
    banners = _active_banners(db)
    return {
        "items": [_serialize_banner(banner) for banner in banners],
        "total_count": len(banners),
    }


@router.get("/catalog/banners")
def list_catalog_banners(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [_serialize_banner(banner) for banner in _active_banners(db)]


def _normalize_event_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_events = payload.get("events")
    if raw_events is None:
        raw_events = [payload]
    if not isinstance(raw_events, list) or not raw_events:
        raise api_error(400, "EMPTY_EVENTS", "events must be a non-empty array")
    if any(not isinstance(item, dict) for item in raw_events):
        raise api_error(400, "INVALID_REQUEST", "Each event must be an object")
    return raw_events


def _normalize_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise api_error(400, "INVALID_REQUEST", f"{field_name} is required")
    try:
        return str(UUID(value))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", f"{field_name} must be a valid UUID")


def _normalize_timestamp(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, str):
        raise api_error(400, "INVALID_REQUEST", "timestamp must be a date-time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise api_error(400, "INVALID_REQUEST", "timestamp must be a date-time")
    if parsed.tzinfo is None:
        raise api_error(400, "INVALID_REQUEST", "timestamp must include timezone")
    return parsed


@router.post("/banner-events", status_code=202)
def record_banner_events(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
) -> dict[str, int]:
    events = _normalize_event_payload(payload)
    banner_ids = [
        _normalize_uuid(event.get("banner_id"), "banner_id")
        for event in events
    ]
    known_banner_ids = {
        banner.id
        for banner in db.query(Banner).filter(Banner.id.in_(set(banner_ids))).all()
    }
    if any(banner_id not in known_banner_ids for banner_id in banner_ids):
        raise api_error(400, "BANNER_NOT_FOUND", "Banner not found")

    records: list[BannerEvent] = []
    for event, banner_id in zip(events, banner_ids, strict=True):
        event_type = event.get("event", event.get("event_type"))
        if event_type not in ALLOWED_BANNER_EVENTS:
            raise api_error(
                400,
                "INVALID_EVENT",
                "event must be impression or click",
            )
        user_id = event.get("user_id")
        records.append(
            BannerEvent(
                banner_id=banner_id,
                user_id=(
                    _normalize_uuid(user_id, "user_id")
                    if user_id is not None
                    else None
                ),
                event=event_type,
                timestamp=_normalize_timestamp(event.get("timestamp")),
            )
        )
    db.add_all(records)
    db.commit()
    return {"accepted": len(records)}
