from typing import Any

from fastapi.testclient import TestClient

from app.b2b_client import B2BUnavailableError, get_b2b_client


IOS_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
ANDROID_CATEGORY_ID = "c47ac10b-58cc-4372-a567-0e02b2c3d470"


class FakeB2BClient:
    def __init__(self, products: list[dict[str, Any]] | None = None) -> None:
        self.products = products or []
        self.calls: list[dict[str, Any]] = []

    def list_products(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(params)
        return {
            "items": self.products,
            "total_count": len(self.products),
            "limit": params.get("limit", 100),
            "offset": params.get("offset", 0),
        }


class UnavailableB2BClient:
    def list_products(self, params: dict[str, Any]) -> dict[str, Any]:
        raise B2BUnavailableError


def override_b2b(client: TestClient, fake_b2b: Any) -> Any:
    client.app.dependency_overrides[get_b2b_client] = lambda: fake_b2b
    return fake_b2b


def catalog_product(
    product_id: str,
    title: str,
    category_id: str,
    price: int,
    brand: str,
    color: str,
    active_quantity: int = 3,
    discount: int = 0,
) -> dict[str, Any]:
    return {
        "id": product_id,
        "title": title,
        "description": f"{title} description",
        "status": "MODERATED",
        "category": {"id": category_id, "name": "iOS"},
        "images": [{"url": f"/s3/{product_id}.jpg", "ordering": 0}],
        "characteristics": [{"name": "brand", "value": brand}],
        "skus": [
            {
                "id": f"{product_id}-sku",
                "product_id": product_id,
                "name": "Base",
                "price": price,
                "discount": discount,
                "image": f"/s3/{product_id}-sku.jpg",
                "active_quantity": active_quantity,
                "characteristics": [{"name": "color", "value": color}],
            }
        ],
        "created_at": "2026-03-15T10:00:00.000Z",
    }


def test_catalog_returns_filtered_sorted_products(client: TestClient) -> None:
    products = [
        catalog_product(
            "00000000-0000-0000-0000-000000000001",
            "iPhone 15",
            IOS_CATEGORY_ID,
            12999000,
            "Apple",
            "black",
        ),
        catalog_product(
            "00000000-0000-0000-0000-000000000002",
            "iPhone 14",
            IOS_CATEGORY_ID,
            9999000,
            "Apple",
            "white",
        ),
        catalog_product(
            "00000000-0000-0000-0000-000000000003",
            "Pixel 8",
            ANDROID_CATEGORY_ID,
            8999000,
            "Google",
            "black",
        ),
    ]
    fake_b2b = override_b2b(client, FakeB2BClient(products))

    response = client.get(
        "/api/v1/products",
        params={
            "category_id": IOS_CATEGORY_ID,
            "filters[brand]": "Apple",
            "sort": "price_asc",
            "limit": 1,
            "offset": 0,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "items": [
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "title": "iPhone 14",
                "image": "/s3/00000000-0000-0000-0000-000000000002.jpg",
                "price": 9999000,
                "in_stock": True,
                "is_in_cart": False,
            }
        ],
        "total_count": 2,
        "limit": 1,
        "offset": 0,
    }
    assert fake_b2b.calls[0]["category_id"] == IOS_CATEGORY_ID
    assert fake_b2b.calls[0]["limit"] == 100


def test_facets_return_counts_per_filter_value(client: TestClient) -> None:
    products = [
        catalog_product(
            "00000000-0000-0000-0000-000000000001",
            "iPhone 15",
            IOS_CATEGORY_ID,
            12999000,
            "Apple",
            "black",
        ),
        catalog_product(
            "00000000-0000-0000-0000-000000000002",
            "Galaxy S24",
            IOS_CATEGORY_ID,
            8999000,
            "Samsung",
            "black",
        ),
        catalog_product(
            "00000000-0000-0000-0000-000000000003",
            "iPhone 14",
            IOS_CATEGORY_ID,
            9999000,
            "Apple",
            "white",
        ),
    ]
    override_b2b(client, FakeB2BClient(products))

    response = client.get(
        "/api/v1/catalog/facets",
        params={"category_id": IOS_CATEGORY_ID},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["category_id"] == IOS_CATEGORY_ID
    assert {
        "name": "brand",
        "values": [
            {"value": "Apple", "count": 2},
            {"value": "Samsung", "count": 1},
        ],
    } in body["facets"]
    assert {
        "name": "color",
        "values": [
            {"value": "black", "count": 2},
            {"value": "white", "count": 1},
        ],
    } in body["facets"]


def test_invalid_sort_returns_400(client: TestClient) -> None:
    override_b2b(client, FakeB2BClient([]))

    response = client.get("/api/v1/products", params={"sort": "price_sideways"})

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": (
            "Invalid sort parameter. Allowed: rating, popularity, price_asc, "
            "price_desc, date_desc, discount_desc"
        ),
    }


def test_b2b_unavailable_returns_502(client: TestClient) -> None:
    override_b2b(client, UnavailableB2BClient())

    response = client.get("/api/v1/products")

    assert response.status_code == 502
    assert response.json() == {
        "code": "B2B_UNAVAILABLE",
        "message": "Catalog is temporarily unavailable",
    }
