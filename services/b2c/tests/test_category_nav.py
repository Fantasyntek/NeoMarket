from typing import Any

from fastapi.testclient import TestClient

from app.b2b_client import get_b2b_client


ROOT_ID = "11111111-1111-1111-1111-111111111111"
SMARTPHONES_ID = "22222222-2222-2222-2222-222222222222"
IOS_ID = "33333333-3333-3333-3333-333333333333"
ANDROID_ID = "44444444-4444-4444-4444-444444444444"
PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"


class FakeB2BClient:
    def __init__(
        self,
        categories: list[dict[str, Any]],
        product: dict[str, Any] | None = None,
    ) -> None:
        self.categories = categories
        self.product = product

    def list_products(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"items": [], "total_count": 0, "limit": 20, "offset": 0}

    def get_product(self, product_id: str) -> dict[str, Any]:
        return self.product or {
            "id": product_id,
            "category": {"id": IOS_ID, "name": "iOS"},
            "status": "MODERATED",
            "deleted": False,
        }

    def list_categories(self) -> dict[str, Any]:
        return {"items": self.categories}


def override_b2b(client: TestClient, fake_b2b: FakeB2BClient) -> FakeB2BClient:
    client.app.dependency_overrides[get_b2b_client] = lambda: fake_b2b
    return fake_b2b


def category(
    category_id: str,
    name: str,
    parent_id: str | None,
    slug: str,
) -> dict[str, Any]:
    return {
        "id": category_id,
        "name": name,
        "parent_id": parent_id,
        "slug": slug,
        "description": f"{name} category",
        "product_count": 7,
        "seo": {"title": f"{name} | NeoMarket"},
        "meta_tags": {"og_title": name},
        "image_url": f"/s3/categories/{slug}.jpg",
        "is_active": True,
        "created_at": "2026-03-15T10:00:00Z",
        "updated_at": "2026-03-16T10:00:00Z",
    }


def valid_categories() -> list[dict[str, Any]]:
    return [
        category(ROOT_ID, "Electronics", None, "electronics"),
        category(SMARTPHONES_ID, "Smartphones", ROOT_ID, "smartphones"),
        category(IOS_ID, "iOS", SMARTPHONES_ID, "ios"),
        category(ANDROID_ID, "Android", SMARTPHONES_ID, "android"),
    ]


def test_category_tree_returns_nested_structure(client: TestClient) -> None:
    override_b2b(client, FakeB2BClient(valid_categories()))

    response = client.get("/api/v1/categories")

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "id": ROOT_ID,
            "name": "Electronics",
            "parent_id": None,
            "children": [
                {
                    "id": SMARTPHONES_ID,
                    "name": "Smartphones",
                    "parent_id": ROOT_ID,
                    "children": [
                        {
                            "id": ANDROID_ID,
                            "name": "Android",
                            "parent_id": SMARTPHONES_ID,
                            "children": [],
                        },
                        {
                            "id": IOS_ID,
                            "name": "iOS",
                            "parent_id": SMARTPHONES_ID,
                            "children": [],
                        },
                    ],
                }
            ],
        }
    ]


def test_category_detail_returns_category_payload(client: TestClient) -> None:
    override_b2b(client, FakeB2BClient(valid_categories()))

    response = client.get(f"/api/v1/categories/{IOS_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == IOS_ID
    assert body["name"] == "iOS"
    assert body["slug"] == "ios"
    assert body["parent"] == {
        "id": SMARTPHONES_ID,
        "name": "Smartphones",
        "slug": "smartphones",
    }
    assert body["product_count"] == 7


def test_breadcrumbs_return_path_from_root(client: TestClient) -> None:
    override_b2b(client, FakeB2BClient(valid_categories()))

    response = client.get("/api/v1/breadcrumbs", params={"category_id": IOS_ID})

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["data"]] == [ROOT_ID, SMARTPHONES_ID, IOS_ID]
    assert body["data"][-1]["is_current"] is True
    assert body["data"][-1]["url"] == "/catalog/ios"
    assert body["meta"] == {"resolved_via": "category_id", "category_id": IOS_ID}


def test_breadcrumbs_can_resolve_product_category(client: TestClient) -> None:
    override_b2b(
        client,
        FakeB2BClient(
            valid_categories(),
            product={
                "id": PRODUCT_ID,
                "category": {"id": IOS_ID, "name": "iOS"},
                "status": "MODERATED",
                "deleted": False,
            },
        ),
    )

    response = client.get("/api/v1/breadcrumbs", params={"product_id": PRODUCT_ID})

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["data"]] == [ROOT_ID, SMARTPHONES_ID, IOS_ID]
    assert body["meta"] == {"resolved_via": "product_id", "category_id": IOS_ID}


def test_unknown_category_returns_404(client: TestClient) -> None:
    override_b2b(client, FakeB2BClient(valid_categories()))

    response = client.get(
        "/api/v1/breadcrumbs",
        params={"category_id": "99999999-9999-9999-9999-999999999999"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "NOT_FOUND",
        "message": "Category not found",
    }


def test_ambiguous_params_returns_400(client: TestClient) -> None:
    override_b2b(client, FakeB2BClient(valid_categories()))

    response = client.get(
        "/api/v1/breadcrumbs",
        params={"category_id": IOS_ID, "product_id": PRODUCT_ID},
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "only one of category_id or product_id must be provided",
    }


def test_orphan_node_returns_422(client: TestClient) -> None:
    broken_categories = [
        category(ROOT_ID, "Electronics", None, "electronics"),
        category(IOS_ID, "iOS", "99999999-9999-9999-9999-999999999999", "ios"),
    ]
    override_b2b(client, FakeB2BClient(broken_categories))

    response = client.get("/api/v1/categories")

    assert response.status_code == 422
    assert response.json() == {
        "code": "ORPHAN_NODE",
        "message": "category hierarchy is broken",
    }
