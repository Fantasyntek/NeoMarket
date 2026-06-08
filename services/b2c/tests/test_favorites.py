from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.b2b_client import B2BUnavailableError, get_b2b_client
from app.models import Favorite


USER_ID = "f3d4e5f6-a7b8-4012-8def-123456789012"
OTHER_USER_ID = "a3d4e5f6-a7b8-4012-8def-123456789099"
PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"
BLOCKED_PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440003"


def product_payload(product_id: str = PRODUCT_ID) -> dict[str, Any]:
    return {
        "id": product_id,
        "title": "iPhone 15 Pro Max",
        "slug": "iphone-15-pro-max",
        "description": "Flagship Apple smartphone",
        "status": "MODERATED",
        "deleted": False,
        "category": {
            "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "name": "Smartphones",
        },
        "images": [
            {
                "id": "870e8400-e29b-41d4-a716-446655440002",
                "url": "/s3/iphone-front.jpg",
                "ordering": 0,
            }
        ],
        "skus": [
            {
                "id": "660e8400-e29b-41d4-a716-446655440001",
                "name": "256GB Black",
                "price": 12999000,
                "discount": 500000,
                "image": "/s3/iphone-black.jpg",
                "active_quantity": 10,
                "characteristics": [],
            }
        ],
    }


class FakeB2BClient:
    def __init__(
        self,
        visible_products: list[dict[str, Any]] | None = None,
    ) -> None:
        self.visible_products = visible_products or [product_payload()]
        self.get_calls: list[str] = []
        self.batch_calls: list[list[str]] = []

    def get_product(self, product_id: str) -> dict[str, Any]:
        self.get_calls.append(product_id)
        for product in self.visible_products:
            if product["id"] == product_id:
                return product
        raise AssertionError(f"Unexpected product lookup: {product_id}")

    def batch_products(self, product_ids: list[str]) -> list[dict[str, Any]]:
        self.batch_calls.append(product_ids)
        requested = set(product_ids)
        return [
            product
            for product in self.visible_products
            if product["id"] in requested
        ]


class UnavailableB2BClient:
    def get_product(self, product_id: str) -> dict[str, Any]:
        raise B2BUnavailableError

    def batch_products(self, product_ids: list[str]) -> list[dict[str, Any]]:
        raise B2BUnavailableError


def override_b2b(client: TestClient, fake_b2b: Any) -> Any:
    client.app.dependency_overrides[get_b2b_client] = lambda: fake_b2b
    return fake_b2b


def headers_for(user_id: str) -> dict[str, str]:
    token = create_access_token({"sub": user_id})
    return {"Authorization": f"Bearer {token}"}


def add_favorite(
    db_session: Session,
    product_id: str,
    user_id: str = USER_ID,
) -> Favorite:
    favorite = Favorite(user_id=user_id, product_id=product_id)
    db_session.add(favorite)
    db_session.commit()
    db_session.refresh(favorite)
    return favorite


def test_add_to_favorites_returns_201(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    fake_b2b = override_b2b(client, FakeB2BClient())

    response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}",
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert response.json()["product_id"] == PRODUCT_ID
    assert response.json()["added_at"]
    favorite = db_session.query(Favorite).one()
    assert favorite.user_id == USER_ID
    assert fake_b2b.get_calls == [PRODUCT_ID]


def test_repeat_add_returns_200_not_duplicate(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(client, FakeB2BClient())

    first_response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}",
        headers=auth_headers,
    )
    second_response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}",
        headers=auth_headers,
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 200
    assert second_response.json()["added_at"] == first_response.json()["added_at"]
    assert db_session.query(Favorite).count() == 1


def test_get_favorites_enriched_from_b2b(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    add_favorite(db_session, PRODUCT_ID)
    fake_b2b = override_b2b(client, FakeB2BClient())

    response = client.get("/api/v1/favorites", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": PRODUCT_ID,
                "name": "iPhone 15 Pro Max",
                "slug": "iphone-15-pro-max",
                "min_price": 12499000,
                "old_price": 12999000,
                "has_stock": True,
                "rating": None,
                "reviews_count": 0,
                "images": [
                    {
                        "id": "870e8400-e29b-41d4-a716-446655440002",
                        "url": "/s3/iphone-front.jpg",
                        "ordering": 0,
                    }
                ],
                "category": {
                    "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
                    "name": "Smartphones",
                    "parent_id": None,
                    "level": 0,
                    "path": ["Smartphones"],
                },
            }
        ],
        "total_count": 1,
        "limit": 20,
        "offset": 0,
    }
    assert fake_b2b.batch_calls == [[PRODUCT_ID]]


def test_blocked_product_excluded_from_list(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    add_favorite(db_session, PRODUCT_ID)
    add_favorite(db_session, BLOCKED_PRODUCT_ID)
    override_b2b(client, FakeB2BClient([product_payload(PRODUCT_ID)]))

    response = client.get("/api/v1/favorites", headers=auth_headers)

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [PRODUCT_ID]
    assert response.json()["total_count"] == 1
    assert db_session.query(Favorite).count() == 2


def test_user_id_from_query_is_ignored(
    client: TestClient,
    db_session: Session,
) -> None:
    override_b2b(client, FakeB2BClient())

    response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}?user_id={OTHER_USER_ID}",
        headers=headers_for(USER_ID),
    )

    assert response.status_code == 201
    favorite = db_session.query(Favorite).one()
    assert favorite.user_id == USER_ID
    assert favorite.user_id != OTHER_USER_ID


def test_delete_nonexistent_favorite_returns_204(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    response = client.delete(
        f"/api/v1/favorites/{PRODUCT_ID}",
        headers=auth_headers,
    )

    assert response.status_code == 204
    assert response.content == b""


def test_favorites_are_isolated_by_jwt_user(
    client: TestClient,
    db_session: Session,
) -> None:
    add_favorite(db_session, PRODUCT_ID, user_id=OTHER_USER_ID)
    override_b2b(client, FakeB2BClient())

    response = client.get(
        f"/api/v1/favorites?user_id={OTHER_USER_ID}",
        headers=headers_for(USER_ID),
    )

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total_count"] == 0


def test_b2b_unavailable_returns_503(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    override_b2b(client, UnavailableB2BClient())

    response = client.post(
        f"/api/v1/favorites/{PRODUCT_ID}",
        headers=auth_headers,
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "B2B_UNAVAILABLE",
        "message": "Catalog is temporarily unavailable",
    }


def test_get_favorites_b2b_unavailable_returns_503(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    add_favorite(db_session, PRODUCT_ID)
    override_b2b(client, UnavailableB2BClient())

    response = client.get("/api/v1/favorites", headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["code"] == "B2B_UNAVAILABLE"
