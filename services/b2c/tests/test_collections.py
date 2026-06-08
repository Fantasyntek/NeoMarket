from datetime import date, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.b2b_client import get_b2b_client
from app.models import Collection, CollectionProduct


PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440002"
UNAVAILABLE_PRODUCT_ID = "770e8400-e29b-41d4-a716-446655440003"
UNKNOWN_COLLECTION_ID = "550e8400-e29b-41d4-a716-446655449999"


def product_payload(product_id: str = PRODUCT_ID) -> dict[str, Any]:
    return {
        "id": product_id,
        "title": "iPhone 15 Pro Max",
        "slug": "iphone-15-pro-max",
        "status": "MODERATED",
        "deleted": False,
        "category": {
            "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "name": "Smartphones",
        },
        "images": [
            {
                "id": "870e8400-e29b-41d4-a716-446655440002",
                "url": "/s3/iphone.jpg",
                "ordering": 0,
            }
        ],
        "skus": [
            {
                "id": "660e8400-e29b-41d4-a716-446655440001",
                "price": 12999000,
                "discount": 0,
                "active_quantity": 5,
            }
        ],
    }


class FakeB2BClient:
    def __init__(self, products: list[dict[str, Any]] | None = None) -> None:
        self.products = products if products is not None else [product_payload()]
        self.batch_calls: list[list[str]] = []

    def batch_products(self, product_ids: list[str]) -> list[dict[str, Any]]:
        self.batch_calls.append(product_ids)
        requested = set(product_ids)
        return [
            product for product in self.products if product["id"] in requested
        ]


def override_b2b(client: TestClient, fake_b2b: FakeB2BClient) -> FakeB2BClient:
    client.app.dependency_overrides[get_b2b_client] = lambda: fake_b2b
    return fake_b2b


def create_collection(
    db_session: Session,
    title: str = "Best sellers",
    *,
    priority: int = 10,
    is_active: bool = True,
    start_date: date | None = None,
) -> Collection:
    collection = Collection(
        title=title,
        description=f"{title} description",
        cover_image_url="/cdn/collection.jpg",
        target_url="/catalog?collection=best",
        priority=priority,
        is_active=is_active,
        start_date=start_date,
    )
    db_session.add(collection)
    db_session.commit()
    db_session.refresh(collection)
    return collection


def add_product(
    db_session: Session,
    collection: Collection,
    product_id: str,
    ordering: int,
) -> None:
    db_session.add(
        CollectionProduct(
            collection_id=collection.id,
            product_id=product_id,
            ordering=ordering,
        )
    )
    db_session.commit()


def test_collections_list_returns_metadata_without_products(
    client: TestClient,
    db_session: Session,
) -> None:
    second = create_collection(db_session, "Second", priority=20)
    first = create_collection(db_session, "First", priority=10)
    add_product(db_session, first, PRODUCT_ID, 0)
    create_collection(db_session, "Disabled", priority=1, is_active=False)
    create_collection(
        db_session,
        "Future",
        priority=2,
        start_date=date.today() + timedelta(days=1),
    )

    response = client.get("/api/v1/main/collections")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [
        first.id,
        second.id,
    ]
    assert response.json()["total_count"] == 2
    assert all("products" not in item for item in response.json()["items"])
    assert response.json()["collections"] == response.json()["items"]
    assert response.json()["metadata"]["total_count"] == 2


def test_collection_products_enriched_from_b2b(
    client: TestClient,
    db_session: Session,
) -> None:
    collection = create_collection(db_session)
    add_product(db_session, collection, PRODUCT_ID, 0)
    fake_b2b = override_b2b(client, FakeB2BClient())

    response = client.get(f"/api/v1/collections/{collection.id}/products")

    assert response.status_code == 200
    assert response.json()["collection_id"] == collection.id
    assert response.json()["items"][0]["id"] == PRODUCT_ID
    assert response.json()["items"][0]["name"] == "iPhone 15 Pro Max"
    assert response.json()["unavailable_ids"] == []
    assert fake_b2b.batch_calls == [[PRODUCT_ID]]


def test_unavailable_products_in_unavailable_ids(
    client: TestClient,
    db_session: Session,
) -> None:
    collection = create_collection(db_session)
    add_product(db_session, collection, PRODUCT_ID, 0)
    add_product(db_session, collection, UNAVAILABLE_PRODUCT_ID, 1)
    override_b2b(client, FakeB2BClient([product_payload(PRODUCT_ID)]))

    response = client.get(f"/api/v1/collections/{collection.id}/products")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [PRODUCT_ID]
    assert response.json()["unavailable_ids"] == [UNAVAILABLE_PRODUCT_ID]
    assert response.json()["total_products"] == 2


def test_all_products_unavailable_returns_valid_empty_items(
    client: TestClient,
    db_session: Session,
) -> None:
    collection = create_collection(db_session)
    add_product(db_session, collection, UNAVAILABLE_PRODUCT_ID, 0)
    override_b2b(client, FakeB2BClient([]))

    response = client.get(f"/api/v1/collections/{collection.id}/products")

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["unavailable_ids"] == [UNAVAILABLE_PRODUCT_ID]


def test_unknown_collection_returns_404(client: TestClient) -> None:
    response = client.get(
        f"/api/v1/collections/{UNKNOWN_COLLECTION_ID}/products"
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "COLLECTION_NOT_FOUND",
        "message": "Collection not found",
    }


def test_catalog_collections_alias_matches_openapi(
    client: TestClient,
    db_session: Session,
) -> None:
    collection = create_collection(db_session)

    response = client.get("/api/v1/catalog/collections")

    assert response.status_code == 200
    assert response.json()[0]["id"] == collection.id
    assert response.json()[0]["name"] == collection.title
    assert response.json()[0]["products"] == []


def test_inactive_collection_products_return_404(
    client: TestClient,
    db_session: Session,
) -> None:
    collection = create_collection(db_session, is_active=False)

    response = client.get(f"/api/v1/collections/{collection.id}/products")

    assert response.status_code == 404
    assert response.json()["code"] == "COLLECTION_NOT_FOUND"
