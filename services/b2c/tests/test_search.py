from typing import Any

from fastapi.testclient import TestClient

from app.b2b_client import get_b2b_client


IOS_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


class FakeB2BClient:
    def __init__(self, products: list[dict[str, Any]]) -> None:
        self.products = products
        self.calls: list[dict[str, Any]] = []

    def list_products(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(params)
        search = str(params.get("search", "")).lower()
        products = self.products
        if search:
            products = [
                product
                for product in products
                if search in str(product.get("title", "")).lower()
                or search in str(product.get("description", "")).lower()
            ]
        return {
            "items": products,
            "total_count": len(products),
            "limit": params.get("limit", 100),
            "offset": params.get("offset", 0),
        }


def override_b2b(client: TestClient, fake_b2b: FakeB2BClient) -> FakeB2BClient:
    client.app.dependency_overrides[get_b2b_client] = lambda: fake_b2b
    return fake_b2b


def catalog_product(
    product_id: str,
    title: str,
    description: str,
    brand: str = "Apple",
) -> dict[str, Any]:
    return {
        "id": product_id,
        "title": title,
        "description": description,
        "status": "MODERATED",
        "category": {"id": IOS_CATEGORY_ID, "name": "iOS"},
        "images": [{"id": product_id, "url": f"/s3/{product_id}.jpg", "ordering": 0}],
        "characteristics": [{"name": "brand", "value": brand}],
        "skus": [
            {
                "id": f"{product_id}-sku",
                "product_id": product_id,
                "name": "Base",
                "price": 12999000,
                "discount": 0,
                "image": f"/s3/{product_id}-sku.jpg",
                "active_quantity": 5,
                "characteristics": [{"name": "color", "value": "black"}],
            }
        ],
        "created_at": "2026-03-15T10:00:00.000Z",
    }


def test_search_returns_matching_products(client: TestClient) -> None:
    products = [
        catalog_product(
            "00000000-0000-0000-0000-000000000001",
            "iPhone 15 Pro",
            "Flagship smartphone",
        ),
        catalog_product(
            "00000000-0000-0000-0000-000000000002",
            "Ceramic Mug",
            "A useful accessory for iPhone owners",
        ),
        catalog_product(
            "00000000-0000-0000-0000-000000000003",
            "Galaxy S24",
            "Android smartphone",
            brand="Samsung",
        ),
    ]
    fake_b2b = override_b2b(client, FakeB2BClient(products))

    response = client.get("/api/v1/catalog/products", params={"q": "iphone"})

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    ]
    assert body["total_count"] == 2
    assert fake_b2b.calls[0]["search"] == "iphone"


def test_short_query_returns_400(client: TestClient) -> None:
    fake_b2b = override_b2b(client, FakeB2BClient([]))

    response = client.get("/api/v1/catalog/products", params={"q": "ip"})

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "Search query must be at least 3 characters",
    }
    assert fake_b2b.calls == []


def test_special_chars_do_not_break_query(client: TestClient) -> None:
    product = catalog_product(
        "00000000-0000-0000-0000-000000000004",
        "iPhone%15 protective case",
        "Case that handles coffee' spills",
    )
    fake_b2b = override_b2b(client, FakeB2BClient([product]))

    response = client.get("/api/v1/catalog/products", params={"q": "iPhone%15"})

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["id"] == product["id"]
    assert body["total_count"] == 1
    assert fake_b2b.calls[0]["search"] == "iPhone%15"


def test_empty_results_returns_200(client: TestClient) -> None:
    products = [
        catalog_product(
            "00000000-0000-0000-0000-000000000005",
            "Galaxy S24",
            "Android smartphone",
            brand="Samsung",
        )
    ]
    fake_b2b = override_b2b(client, FakeB2BClient(products))

    response = client.get("/api/v1/catalog/products", params={"q": "iphone"})

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "total_count": 0,
        "limit": 20,
        "offset": 0,
    }
    assert fake_b2b.calls[0]["search"] == "iphone"
