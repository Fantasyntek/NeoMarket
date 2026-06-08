from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Banner, BannerEvent


UNKNOWN_BANNER_ID = "550e8400-e29b-41d4-a716-446655449999"


def create_banner(
    db_session: Session,
    title: str,
    priority: int,
    *,
    is_active: bool = True,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> Banner:
    banner = Banner(
        title=title,
        image_url=f"/cdn/{title.lower().replace(' ', '-')}.jpg",
        link="/catalog",
        priority=priority,
        is_active=is_active,
        start_at=start_at,
        end_at=end_at,
    )
    db_session.add(banner)
    db_session.commit()
    db_session.refresh(banner)
    return banner


def test_active_banners_returned_sorted_by_priority(
    client: TestClient,
    db_session: Session,
) -> None:
    now = datetime.now(timezone.utc)
    low_priority = create_banner(
        db_session,
        "Second banner",
        20,
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(days=1),
    )
    high_priority = create_banner(
        db_session,
        "First banner",
        10,
        start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=1),
    )
    create_banner(db_session, "Disabled", 1, is_active=False)
    create_banner(
        db_session,
        "Future",
        2,
        start_at=now + timedelta(days=1),
    )
    create_banner(
        db_session,
        "Expired",
        3,
        end_at=now - timedelta(days=1),
    )

    response = client.get("/api/v1/home/banners")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [
        high_priority.id,
        low_priority.id,
    ]
    assert response.json()["total_count"] == 2
    assert response.json()["items"][0]["priority"] == 10


def test_no_active_banners_returns_200_empty(
    client: TestClient,
    db_session: Session,
) -> None:
    create_banner(db_session, "Disabled", 1, is_active=False)

    response = client.get("/api/v1/home/banners")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total_count": 0}


def test_click_on_unknown_banner_returns_400(client: TestClient) -> None:
    response = client.post(
        "/api/v1/banner-events",
        json={
            "events": [
                {
                    "banner_id": UNKNOWN_BANNER_ID,
                    "event": "click",
                    "timestamp": "2026-06-08T10:00:00Z",
                }
            ]
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "BANNER_NOT_FOUND",
        "message": "Banner not found",
    }


def test_banner_events_are_recorded_as_batch(
    client: TestClient,
    db_session: Session,
) -> None:
    banner = create_banner(db_session, "Campaign", 10)

    response = client.post(
        "/api/v1/banner-events",
        json={
            "events": [
                {"banner_id": banner.id, "event": "impression"},
                {"banner_id": banner.id, "event": "click"},
            ]
        },
    )

    assert response.status_code == 202
    assert response.json() == {"accepted": 2}
    assert [
        event.event
        for event in db_session.query(BannerEvent).order_by(BannerEvent.created_at).all()
    ] == ["impression", "click"]


def test_empty_banner_events_returns_400(client: TestClient) -> None:
    response = client.post("/api/v1/banner-events", json={"events": []})

    assert response.status_code == 400
    assert response.json()["code"] == "EMPTY_EVENTS"


def test_invalid_batch_does_not_store_partial_events(
    client: TestClient,
    db_session: Session,
) -> None:
    banner = create_banner(db_session, "Atomic Campaign", 10)

    response = client.post(
        "/api/v1/banner-events",
        json={
            "events": [
                {"banner_id": banner.id, "event": "impression"},
                {"banner_id": banner.id, "event": "unknown"},
            ]
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_EVENT"
    assert db_session.query(BannerEvent).count() == 0


def test_catalog_banner_alias_matches_openapi(
    client: TestClient,
    db_session: Session,
) -> None:
    banner = create_banner(db_session, "OpenAPI Banner", 5)

    response = client.get("/api/v1/catalog/banners")

    assert response.status_code == 200
    assert response.json()[0]["id"] == banner.id
    assert response.json()[0]["ordering"] == 5
