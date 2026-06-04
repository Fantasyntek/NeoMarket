from typing import Any

from fastapi.testclient import TestClient

from app.b2b_client import B2BResponseError, get_b2b_client


CURRENT_PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"
CHILD_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
PARENT_CATEGORY_ID = "a47ac10b-58cc-4372-a567-0e02b2c3d471"


class FakeB2BClient:
    def __init__(
        self,
        current_product: dict[str, Any] | None,
        products_by_category: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.current_product = current_product
        self.products_by_category = products_by_category or {}
        self.product_requests: list[str] = []
        self.list_requests: list[dict[str, Any]] = []

    def list_products(self, params: dict[str, Any]) -> dict[str, Any]:
        self.list_requests.append(params)
        category_id = str(params.get("category", ""))
        products = self.products_by_category.get(category_id, [])
        return {
            "items": products,
            "total_count": len(products),
            "limit": params.get("limit", 100),
            "offset": params.get("offset", 0),
        }

    def get_product(self, product_id: str) -> dict[str, Any]:
        self.product_requests.append(product_id)
        if self.current_product is None:
            raise B2BResponseError(
                404,
                {"code": "NOT_FOUND", "message": "Product not found"},
            )
        return self.current_product


def override_b2b(client: TestClient, fake_b2b: FakeB2BClient) -> FakeB2BClient:
    client.app.dependency_overrides[get_b2b_client] = lambda: fake_b2b
    return fake_b2b


def catalog_product(
    product_id: str,
    category_id: str = CHILD_CATEGORY_ID,
    parent_category_id: str | None = PARENT_CATEGORY_ID,
) -> dict[str, Any]:
    category: dict[str, Any] = {"id": category_id, "name": "Smartphones"}
    if parent_category_id is not None:
        category["parent_id"] = parent_category_id

    return {
        "id": product_id,
        "title": f"Product {product_id[-2:]}",
        "description": "Visible product",
        "status": "MODERATED",
        "deleted": False,
        "category": category,
        "images": [{"url": f"/s3/{product_id}.jpg", "ordering": 0}],
        "characteristics": [{"name": "Brand", "value": "Apple"}],
        "skus": [
            {
                "id": f"{product_id}-sku",
                "product_id": product_id,
                "name": "Base",
                "price": 12999000,
                "discount": 0,
                "image": f"/s3/{product_id}-sku.jpg",
                "active_quantity": 5,
                "characteristics": [{"name": "Color", "value": "Black"}],
            }
        ],
        "created_at": "2026-03-15T10:00:00.000Z",
    }


def test_similar_returns_up_to_8_from_same_category(client: TestClient) -> None:
    current_product = catalog_product(CURRENT_PRODUCT_ID)
    category_products = [current_product] + [
        catalog_product(f"770e8400-e29b-41d4-a716-4466554400{index:02d}")
        for index in range(10, 20)
    ]
    fake_b2b = override_b2b(
        client,
        FakeB2BClient(
            current_product,
            {CHILD_CATEGORY_ID: category_products},
        ),
    )

    response = client.get(f"/api/v1/products/{CURRENT_PRODUCT_ID}/similar")

    assert response.status_code == 200
    body = response.json()
    item_ids = [item["id"] for item in body["items"]]
    assert len(item_ids) == 8
    assert CURRENT_PRODUCT_ID not in item_ids
    assert body["total_count"] == 10
    assert body["limit"] == 8
    assert body["offset"] == 0
    assert fake_b2b.product_requests == [CURRENT_PRODUCT_ID]
    assert fake_b2b.list_requests[0]["category"] == CHILD_CATEGORY_ID


def test_empty_category_returns_200_empty_list(client: TestClient) -> None:
    current_product = catalog_product(CURRENT_PRODUCT_ID, parent_category_id=None)
    override_b2b(
        client,
        FakeB2BClient(
            current_product,
            {CHILD_CATEGORY_ID: [current_product]},
        ),
    )

    response = client.get(f"/api/v1/products/{CURRENT_PRODUCT_ID}/similar")

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "total_count": 0,
        "limit": 8,
        "offset": 0,
    }


def test_unknown_product_returns_404(client: TestClient) -> None:
    override_b2b(client, FakeB2BClient(None))

    response = client.get(f"/api/v1/products/{CURRENT_PRODUCT_ID}/similar")

    assert response.status_code == 404
    assert response.json() == {
        "code": "NOT_FOUND",
        "message": "Product not found",
    }


def test_similar_fills_from_parent_category_when_needed(client: TestClient) -> None:
    current_product = catalog_product(CURRENT_PRODUCT_ID)
    same_category_product = catalog_product(
        "770e8400-e29b-41d4-a716-446655440010"
    )
    parent_category_product = catalog_product(
        "770e8400-e29b-41d4-a716-446655440011",
        category_id=PARENT_CATEGORY_ID,
        parent_category_id=None,
    )
    fake_b2b = override_b2b(
        client,
        FakeB2BClient(
            current_product,
            {
                CHILD_CATEGORY_ID: [current_product, same_category_product],
                PARENT_CATEGORY_ID: [current_product, parent_category_product],
            },
        ),
    )

    response = client.get(
        f"/api/v1/products/{CURRENT_PRODUCT_ID}/similar",
        params={"limit": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [
        same_category_product["id"],
        parent_category_product["id"],
    ]
    assert [request["category"] for request in fake_b2b.list_requests] == [
        CHILD_CATEGORY_ID,
        PARENT_CATEGORY_ID,
    ]
