from typing import Any

from fastapi.testclient import TestClient

from app.b2b_client import get_b2b_client


PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"


class FakeB2BClient:
    def __init__(self, product: dict[str, Any]) -> None:
        self.product = product
        self.requested_product_ids: list[str] = []

    def list_products(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"items": [], "total_count": 0, "limit": 20, "offset": 0}

    def get_product(self, product_id: str) -> dict[str, Any]:
        self.requested_product_ids.append(product_id)
        return self.product


def override_b2b(client: TestClient, fake_b2b: FakeB2BClient) -> FakeB2BClient:
    client.app.dependency_overrides[get_b2b_client] = lambda: fake_b2b
    return fake_b2b


def product_payload(
    status: str = "MODERATED",
    deleted: bool = False,
) -> dict[str, Any]:
    return {
        "id": PRODUCT_ID,
        "seller_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
        "category_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "slug": "iphone-15-pro-max",
        "title": "iPhone 15 Pro Max",
        "description": "Flagship Apple smartphone",
        "status": status,
        "deleted": deleted,
        "images": [
            {"id": "image-1", "url": "/s3/iphone-front.jpg", "ordering": 0},
            {"id": "image-2", "url": "/s3/iphone-back.jpg", "ordering": 1},
        ],
        "characteristics": [
            {"id": "char-1", "name": "Brand", "value": "Apple"},
        ],
        "skus": [
            {
                "id": "660e8400-e29b-41d4-a716-446655440001",
                "product_id": PRODUCT_ID,
                "name": "256GB Black",
                "price": 12999000,
                "cost_price": 9500000,
                "discount": 0,
                "image": "/s3/iphone-black.jpg",
                "active_quantity": 10,
                "reserved_quantity": 2,
                "characteristics": [
                    {"id": "sku-char-1", "name": "Color", "value": "Black"},
                ],
            },
            {
                "id": "660e8400-e29b-41d4-a716-446655440002",
                "product_id": PRODUCT_ID,
                "name": "256GB White",
                "price": 12999000,
                "cost_price": 9400000,
                "discount": 500000,
                "image": "/s3/iphone-white.jpg",
                "active_quantity": 0,
                "reserved_quantity": 0,
                "characteristics": [
                    {"id": "sku-char-2", "name": "Color", "value": "White"},
                ],
            },
        ],
    }


def test_product_card_returns_full_data_with_skus(client: TestClient) -> None:
    fake_b2b = override_b2b(client, FakeB2BClient(product_payload()))

    response = client.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert response.status_code == 200
    body = response.json()
    assert fake_b2b.requested_product_ids == [PRODUCT_ID]
    assert body["id"] == PRODUCT_ID
    assert body["slug"] == "iphone-15-pro-max"
    assert body["name"] == "iPhone 15 Pro Max"
    assert body["min_price"] == 12999000
    assert body["has_stock"] is True
    assert body["description"] == "Flagship Apple smartphone"
    assert body["images"] == [
        {"id": "image-1", "url": "/s3/iphone-front.jpg", "ordering": 0},
        {"id": "image-2", "url": "/s3/iphone-back.jpg", "ordering": 1},
    ]
    assert body["attributes"] == {"Brand": "Apple"}
    assert body["skus"][0] == {
        "id": "660e8400-e29b-41d4-a716-446655440001",
        "name": "256GB Black",
        "price": 12999000,
        "old_price": None,
        "available_quantity": 10,
        "attributes": {"Color": "Black"},
        "images": [
            {
                "id": "660e8400-e29b-41d4-a716-446655440001",
                "url": "/s3/iphone-black.jpg",
                "ordering": 0,
            }
        ],
    }


def test_cost_price_absent_in_response(client: TestClient) -> None:
    override_b2b(client, FakeB2BClient(product_payload()))

    response = client.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert response.status_code == 200
    first_sku = response.json()["skus"][0]
    assert "cost_price" not in first_sku
    assert "reserved_quantity" not in first_sku
    assert "seller_id" not in response.json()


def test_blocked_product_returns_404(client: TestClient) -> None:
    override_b2b(client, FakeB2BClient(product_payload(status="BLOCKED")))

    response = client.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert response.status_code == 404
    assert response.json() == {
        "code": "NOT_FOUND",
        "message": "Product not found",
    }


def test_deleted_product_returns_404(client: TestClient) -> None:
    override_b2b(client, FakeB2BClient(product_payload(deleted=True)))

    response = client.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert response.status_code == 404
    assert response.json() == {
        "code": "NOT_FOUND",
        "message": "Product not found",
    }


def test_sku_without_stock_is_shown_as_unavailable(client: TestClient) -> None:
    override_b2b(client, FakeB2BClient(product_payload()))

    response = client.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert response.status_code == 200
    unavailable_sku = response.json()["skus"][1]
    assert unavailable_sku["available_quantity"] == 0
    assert unavailable_sku["price"] == 12499000
    assert unavailable_sku["old_price"] == 12999000
