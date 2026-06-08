from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth import create_access_token
from app.database import Base, get_db
from app.main import create_app
from app.models import ProductModeration


MODERATOR_1 = "11111111-2222-4333-8444-555555555555"
MODERATOR_2 = "22222222-3333-4444-8555-666666666666"


def auth_headers(moderator_id: str) -> dict[str, str]:
    token = create_access_token({"moderator_id": moderator_id})
    return {"Authorization": f"Bearer {token}"}


def add_card(
    db: Session,
    *,
    priority: int = 1,
    created_at: datetime | None = None,
    status: str = "PENDING",
    moderator_id: str | None = None,
    claim_expires_at: datetime | None = None,
) -> ProductModeration:
    card = ProductModeration(
        product_id=str(uuid4()),
        seller_id=str(uuid4()),
        category_id=str(uuid4()),
        kind="CREATE",
        status=status,
        queue_priority=priority,
        json_after={"title": "Product"},
        moderator_id=moderator_id,
        claimed_at=created_at if status == "IN_REVIEW" else None,
        claim_expires_at=claim_expires_at,
        date_created=created_at or datetime.now(timezone.utc),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def test_next_returns_oldest_pending(
    client: TestClient,
    db_session: Session,
) -> None:
    now = datetime.now(timezone.utc)
    lower_priority = add_card(db_session, priority=2, created_at=now - timedelta(hours=2))
    oldest_top = add_card(db_session, priority=1, created_at=now - timedelta(hours=1))
    add_card(db_session, priority=1, created_at=now)

    response = client.post(
        "/api/v1/queue/claim",
        headers=auth_headers(MODERATOR_1),
    )

    db_session.refresh(oldest_top)
    db_session.refresh(lower_priority)
    assert response.status_code == 200
    assert response.json()["id"] == oldest_top.id
    assert response.json()["status"] == "IN_REVIEW"
    assert response.json()["assigned_moderator_id"] == MODERATOR_1
    assert response.json()["claimed_at"] is not None
    assert response.json()["claim_expires_at"] is not None
    assert oldest_top.status == "IN_REVIEW"
    assert lower_priority.status == "PENDING"


def test_queue_priority_filter_selects_requested_queue(
    client: TestClient,
    db_session: Session,
) -> None:
    add_card(db_session, priority=1)
    requested = add_card(db_session, priority=3)

    response = client.post(
        "/api/v1/queue/claim",
        json={"queue_priority": 3},
        headers=auth_headers(MODERATOR_1),
    )

    assert response.status_code == 200
    assert response.json()["id"] == requested.id
    assert response.json()["queue_priority"] == 3


def test_empty_queue_returns_204(client: TestClient) -> None:
    response = client.post(
        "/api/v1/queue/claim",
        headers=auth_headers(MODERATOR_1),
    )

    assert response.status_code == 204
    assert response.content == b""


def test_moderator_already_has_in_review_returns_409(
    client: TestClient,
    db_session: Session,
) -> None:
    now = datetime.now(timezone.utc)
    add_card(
        db_session,
        status="IN_REVIEW",
        moderator_id=MODERATOR_1,
        claim_expires_at=now + timedelta(minutes=20),
    )
    add_card(db_session)

    response = client.post(
        "/api/v1/queue/claim",
        headers=auth_headers(MODERATOR_1),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MODERATOR_ALREADY_IN_REVIEW"


def test_expired_claim_returns_to_queue(
    client: TestClient,
    db_session: Session,
) -> None:
    expired = add_card(
        db_session,
        status="IN_REVIEW",
        moderator_id=MODERATOR_1,
        claim_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    response = client.post(
        "/api/v1/queue/claim",
        headers=auth_headers(MODERATOR_2),
    )

    db_session.refresh(expired)
    assert response.status_code == 200
    assert response.json()["id"] == expired.id
    assert expired.moderator_id == MODERATOR_2
    assert expired.status == "IN_REVIEW"


def test_canonical_alias_accepts_queue_id(
    client: TestClient,
    db_session: Session,
) -> None:
    requested = add_card(db_session, priority=4)

    response = client.post(
        "/api/v1/product-moderation/get-next",
        json={"queueId": 4},
        headers=auth_headers(MODERATOR_1),
    )

    assert response.status_code == 200
    assert response.json()["id"] == requested.id


def test_concurrent_two_moderators_get_different_cards(tmp_path) -> None:
    database_path = tmp_path / "queue-race.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with TestingSession() as setup:
        first = add_card(setup)
        second = add_card(setup)
        expected = {first.id, second.id}

    barrier = threading.Barrier(2)
    results: list[tuple[int, str | None]] = []
    result_lock = threading.Lock()

    def worker(moderator_id: str) -> None:
        app = create_app(init_database=False)

        def override_get_db():
            with TestingSession() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        with TestClient(app) as thread_client:
            barrier.wait()
            response = thread_client.post(
                "/api/v1/queue/claim",
                headers=auth_headers(moderator_id),
            )
        with result_lock:
            results.append(
                (
                    response.status_code,
                    response.json().get("id") if response.content else None,
                )
            )

    threads = [
        threading.Thread(target=worker, args=(MODERATOR_1,)),
        threading.Thread(target=worker, args=(MODERATOR_2,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert {status for status, _ in results} == {200}
    assert {card_id for _, card_id in results} == expected
