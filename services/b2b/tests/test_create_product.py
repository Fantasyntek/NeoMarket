from copy import deepcopy

from fastapi.testclient import TestClient


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
OTHER_SELLER_ID = "11111111-1111-1111-1111-111111111111"


def valid_product_payload() -> dict:
    return {
        "title": "iPhone 15 Pro Max",
        "description": "Флагманский смартфон Apple 2024 года с чипом A17 Pro",
        "category_id": TEST_CATEGORY_ID,
        "images": [
            {"url": "/s3/iphone15-front.jpg", "ordering": 0},
            {"url": "/s3/iphone15-back.jpg", "ordering": 1},
        ],
        "characteristics": [
            {"name": "Бренд", "value": "Apple"},
            {"name": "Страна-производитель", "value": "Китай"},
        ],
    }


def test_create_product_returns_201_with_created_status(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/products", json=valid_product_payload(), headers=auth_headers
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "CREATED"
    assert body["skus"] == []
    assert body["deleted"] is False
    assert body["images"][0]["url"] == "/s3/iphone15-front.jpg"


def test_seller_id_taken_from_jwt(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    payload = valid_product_payload()
    payload["seller_id"] = OTHER_SELLER_ID

    response = client.post("/api/v1/products", json=payload, headers=auth_headers)

    assert response.status_code == 201
    assert response.json()["seller_id"] == TEST_SELLER_ID


def test_missing_images_returns_400(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    payload = valid_product_payload()
    payload.pop("images")

    response = client.post("/api/v1/products", json=payload, headers=auth_headers)

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "At least one image is required",
    }


def test_missing_category_returns_400(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    payload = valid_product_payload()
    payload.pop("category_id")

    response = client.post("/api/v1/products", json=payload, headers=auth_headers)

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "category_id is required",
    }


def test_invalid_category_id_returns_400(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    payload = deepcopy(valid_product_payload())
    payload["category_id"] = "00000000-0000-0000-0000-000000000000"

    response = client.post("/api/v1/products", json=payload, headers=auth_headers)

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "Category not found",
    }

