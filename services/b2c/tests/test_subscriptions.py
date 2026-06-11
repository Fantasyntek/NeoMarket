from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.b2b_client import B2BResponseError, get_b2b_client
from app.models import ProductSubscription


USER_ID = "f3d4e5f6-a7b8-4012-8def-123456789012"
OTHER_USER_ID = "a3d4e5f6-a7b8-4012-8def-123456789099"
PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"


class FakeB2BClient:
    def __init__(self, product_exists: bool = True) -> None:
        self.product_exists = product_exists
        self.get_calls: list[str] = []

    def get_product(self, product_id: str) -> dict[str, Any]:
        self.get_calls.append(product_id)
        if not self.product_exists:
            raise B2BResponseError(
                404,
                {"code": "NOT_FOUND", "message": "Product not found"},
            )
        return {
            "id": product_id,
            "status": "MODERATED",
            "deleted": False,
        }


def override_b2b(client: TestClient, fake_b2b: FakeB2BClient) -> FakeB2BClient:
    client.app.dependency_overrides[get_b2b_client] = lambda: fake_b2b
    return fake_b2b


def headers_for(user_id: str) -> dict[str, str]:
    token = create_access_token({"sub": user_id})
    return {"Authorization": f"Bearer {token}"}


def test_subscribe_returns_201_with_notify_on(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    fake_b2b = override_b2b(client, FakeB2BClient())

    response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe",
        json={"notify_on": ["BACK_IN_STOCK", "PRICE_DROP"]},
        headers=auth_headers,
    )

    assert response.status_code == 204
    assert response.content == b""
    subscription = db_session.query(ProductSubscription).one()
    assert subscription.user_id == USER_ID
    assert subscription.notify_on == ["BACK_IN_STOCK", "PRICE_DROP"]
    assert fake_b2b.get_calls == [PRODUCT_ID]


def test_duplicate_subscription_returns_409(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(client, FakeB2BClient())

    first_response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe",
        json={"notify_on": ["BACK_IN_STOCK"]},
        headers=auth_headers,
    )
    second_response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe",
        json={"notify_on": ["PRICE_DROP"]},
        headers=auth_headers,
    )

    assert first_response.status_code == 204
    assert second_response.status_code == 409
    assert second_response.json()["code"] == "SUBSCRIPTION_ALREADY_EXISTS"
    assert db_session.query(ProductSubscription).count() == 1


def test_invalid_notify_on_returns_400(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(client, FakeB2BClient())

    empty_response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe",
        json={"notify_on": []},
        headers=auth_headers,
    )
    invalid_response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe",
        json={"notify_on": ["UNKNOWN_EVENT"]},
        headers=auth_headers,
    )

    assert empty_response.status_code == 400
    assert empty_response.json()["code"] == "INVALID_NOTIFY_ON"
    assert invalid_response.status_code == 400
    assert invalid_response.json()["code"] == "INVALID_NOTIFY_ON"


def test_subscribe_to_unknown_product_returns_404(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(client, FakeB2BClient(product_exists=False))

    response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe",
        json={"notify_on": ["BACK_IN_STOCK"]},
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["code"] == "PRODUCT_NOT_FOUND"
    assert db_session.query(ProductSubscription).count() == 0


def test_unsubscribe_returns_204_and_is_idempotent(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(client, FakeB2BClient())
    client.post(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe",
        json={"notify_on": ["BACK_IN_STOCK"]},
        headers=auth_headers,
    )

    first_response = client.delete(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe",
        headers=auth_headers,
    )
    second_response = client.delete(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe",
        headers=auth_headers,
    )

    assert first_response.status_code == 204
    assert second_response.status_code == 204
    assert db_session.query(ProductSubscription).count() == 0


def test_user_id_from_query_is_ignored_for_subscriptions(
    client: TestClient,
    db_session: Session,
) -> None:
    override_b2b(client, FakeB2BClient())

    response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe?user_id={OTHER_USER_ID}",
        json={"notify_on": ["PRICE_DROP"]},
        headers=headers_for(USER_ID),
    )

    assert response.status_code == 204
    subscription = db_session.query(ProductSubscription).one()
    assert subscription.user_id == USER_ID
    assert subscription.user_id != OTHER_USER_ID


def test_openapi_events_field_uses_canonical_values(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(client, FakeB2BClient())

    response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe",
        json={"events": ["BACK_IN_STOCK", "PRICE_DROP"]},
        headers=auth_headers,
    )

    assert response.status_code == 204
    assert response.content == b""
    subscription = db_session.query(ProductSubscription).one()
    assert subscription.notify_on == ["BACK_IN_STOCK", "PRICE_DROP"]


def test_old_event_aliases_return_400(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(client, FakeB2BClient())

    response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}/subscribe",
        json={"notify_on": ["IN_STOCK", "PRICE_DOWN"]},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_NOTIFY_ON"
