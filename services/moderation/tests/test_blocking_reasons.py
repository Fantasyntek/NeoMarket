from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.models import BlockingReason, ProductModeration


MODERATOR_ID = "11111111-2222-4333-8444-555555555555"


def auth_headers(*, admin: bool = False) -> dict[str, str]:
    claims = {"moderator_id": MODERATOR_ID}
    if admin:
        claims["role"] = "ADMIN"
    token = create_access_token(claims)
    return {"Authorization": f"Bearer {token}"}


def create_reason(
    db: Session,
    *,
    title: str,
    hard_block: bool,
    is_active: bool = True,
) -> BlockingReason:
    reason = BlockingReason(
        code=f"REASON_{uuid4().hex.upper()}",
        title=title,
        description=f"Description for {title}",
        hard_block=hard_block,
        is_active=is_active,
    )
    db.add(reason)
    db.commit()
    db.refresh(reason)
    return reason


def test_list_returns_active_reasons(
    client: TestClient,
    db_session: Session,
) -> None:
    soft_reason = create_reason(
        db_session,
        title="Incorrect product image",
        hard_block=False,
    )
    hard_reason = create_reason(
        db_session,
        title="Counterfeit product",
        hard_block=True,
    )

    response = client.get(
        "/api/v1/product-blocking-reasons",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": hard_reason.id,
            "title": "Counterfeit product",
            "hard_block": True,
        },
        {
            "id": soft_reason.id,
            "title": "Incorrect product image",
            "hard_block": False,
        },
    ]


def test_inactive_reasons_not_visible(
    client: TestClient,
    db_session: Session,
) -> None:
    active = create_reason(
        db_session,
        title="Incorrect category",
        hard_block=False,
    )
    create_reason(
        db_session,
        title="Deprecated reason",
        hard_block=False,
        is_active=False,
    )

    response = client.get(
        "/api/v1/product-blocking-reasons",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [active.id]


def test_hard_block_filter_returns_matching_active_reasons(
    client: TestClient,
    db_session: Session,
) -> None:
    create_reason(
        db_session,
        title="Incorrect description",
        hard_block=False,
    )
    hard_reason = create_reason(
        db_session,
        title="Forbidden goods",
        hard_block=True,
    )

    response = client.get(
        "/api/v1/product-blocking-reasons?hard_block=true",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": hard_reason.id,
            "title": "Forbidden goods",
            "hard_block": True,
        }
    ]


def test_referenced_reason_cannot_be_deleted(
    client: TestClient,
    db_session: Session,
) -> None:
    reason = create_reason(
        db_session,
        title="Copyright violation",
        hard_block=True,
    )
    card = ProductModeration(
        product_id=str(uuid4()),
        seller_id=str(uuid4()),
        status="HARD_BLOCKED",
        queue_priority=1,
        json_after={"title": "Blocked product", "skus": []},
        blocking_reason_id=reason.id,
    )
    db_session.add(card)
    db_session.commit()

    response = client.delete(
        f"/api/v1/blocking-reasons/{reason.id}",
        headers=auth_headers(admin=True),
    )

    db_session.refresh(reason)
    db_session.refresh(card)
    assert response.status_code == 204
    assert reason.is_active is False
    assert card.blocking_reason_id == reason.id
    assert db_session.get(BlockingReason, reason.id) is not None


def test_admin_crud_requires_admin_role(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/blocking-reasons",
        json={
            "code": "COUNTERFEIT_GOODS",
            "title": "Counterfeit goods",
            "hard_block": True,
        },
        headers=auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_openapi_admin_create_update_and_list(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/blocking-reasons",
        json={
            "code": "MISLEADING_DESCRIPTION",
            "title": "Misleading description",
            "description": "Seller can correct the description",
            "hard_block": False,
        },
        headers=auth_headers(admin=True),
    )
    updated = client.patch(
        f"/api/v1/blocking-reasons/{created.json()['id']}",
        json={"title": "Description does not match"},
        headers=auth_headers(admin=True),
    )
    listed = client.get(
        "/api/v1/blocking-reasons?hard_block=false",
        headers=auth_headers(),
    )

    assert created.status_code == 201
    assert updated.status_code == 200
    assert updated.json()["title"] == "Description does not match"
    assert listed.status_code == 200
    assert listed.json()[0]["code"] == "MISLEADING_DESCRIPTION"
    assert listed.json()[0]["is_active"] is True
