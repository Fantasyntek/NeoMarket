from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import CharacteristicValue, Product, ProductImage, SKU


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
SERVICE_HEADERS = {"X-Service-Key": "dev-service-key"}


def create_product_fixture(
    db_session: Session,
    title: str,
    status: str = "MODERATED",
    deleted: bool = False,
) -> Product:
    product = Product(
        seller_id=TEST_SELLER_ID,
        category_id=TEST_CATEGORY_ID,
        title=title,
        slug=title.lower().replace(" ", "-"),
        description=f"{title} description",
        status=status,
        deleted=deleted,
    )
    product.images = [ProductImage(url=f"/s3/{product.slug}.jpg", ordering=0)]
    product.characteristics = [CharacteristicValue(name="Brand", value="Apple")]
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def create_sku_fixture(
    db_session: Session,
    product: Product,
    active_quantity: int,
    name: str = "256GB Black",
) -> SKU:
    sku = SKU(
        product_id=product.id,
        name=name,
        price=12999000,
        cost_price=9500000,
        discount=0,
        image="/s3/iphone15-black-256.jpg",
        active_quantity=active_quantity,
        reserved_quantity=3,
    )
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


def test_catalog_returns_moderated_in_stock_products(
    client: TestClient, db_session: Session
) -> None:
    visible_product = create_product_fixture(db_session, "Visible iPhone")
    create_sku_fixture(db_session, visible_product, active_quantity=5)
    out_of_stock_product = create_product_fixture(db_session, "Out Of Stock iPhone")
    create_sku_fixture(db_session, out_of_stock_product, active_quantity=0)
    blocked_product = create_product_fixture(db_session, "Blocked iPhone", status="BLOCKED")
    create_sku_fixture(db_session, blocked_product, active_quantity=5)
    deleted_product = create_product_fixture(db_session, "Deleted iPhone", deleted=True)
    create_sku_fixture(db_session, deleted_product, active_quantity=5)

    response = client.get("/api/v1/public/products", headers=SERVICE_HEADERS)

    assert response.status_code == 200
    body = response.json()
    item_ids = [item["id"] for item in body["items"]]
    assert item_ids == [visible_product.id]
    assert body["total_count"] == 1
    assert body["items"][0]["skus"][0]["active_quantity"] == 5


def test_catalog_excludes_hard_blocked(
    client: TestClient, db_session: Session
) -> None:
    hard_blocked_product = create_product_fixture(
        db_session,
        "Hard Blocked iPhone",
        status="HARD_BLOCKED",
    )
    create_sku_fixture(db_session, hard_blocked_product, active_quantity=5)

    response = client.get("/api/v1/public/products", headers=SERVICE_HEADERS)

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_catalog_missing_service_key_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/public/products")

    assert response.status_code == 401
    assert response.json() == {
        "code": "UNAUTHORIZED",
        "message": "Invalid service key",
    }


def test_catalog_response_has_no_cost_price(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session, "Visible iPhone")
    create_sku_fixture(db_session, product, active_quantity=5)

    response = client.get("/api/v1/public/products", headers=SERVICE_HEADERS)

    assert response.status_code == 200
    sku_payload = response.json()["items"][0]["skus"][0]
    assert "cost_price" not in sku_payload
    assert "reserved_quantity" not in sku_payload


def test_batch_ids_returns_visible_subset(
    client: TestClient, db_session: Session
) -> None:
    visible_product = create_product_fixture(db_session, "Visible iPhone")
    create_sku_fixture(db_session, visible_product, active_quantity=5)
    hidden_product = create_product_fixture(db_session, "Hidden iPhone", status="BLOCKED")
    create_sku_fixture(db_session, hidden_product, active_quantity=5)
    out_of_stock_product = create_product_fixture(db_session, "Out Of Stock iPhone")
    create_sku_fixture(db_session, out_of_stock_product, active_quantity=0)
    response = client.post(
        "/api/v1/public/products/batch",
        headers=SERVICE_HEADERS,
        json={
            "product_ids": [
                visible_product.id,
                hidden_product.id,
                out_of_stock_product.id,
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == [visible_product.id]


def test_seller_catalog_does_not_accept_service_key(client: TestClient) -> None:
    response = client.get("/api/v1/products", headers=SERVICE_HEADERS)

    assert response.status_code == 401
    assert response.json() == {
        "code": "UNAUTHORIZED",
        "message": "Authorization required",
    }


def test_public_sku_lookup_returns_out_of_stock_sku(
    client: TestClient,
    db_session: Session,
) -> None:
    product = create_product_fixture(db_session, "Out Of Stock iPhone")
    sku = create_sku_fixture(db_session, product, active_quantity=0)

    response = client.get(
        f"/api/v1/public/skus/{sku.id}",
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["product"]["id"] == product.id
    assert response.json()["sku"]["id"] == sku.id
    assert response.json()["sku"]["active_quantity"] == 0
    assert "cost_price" not in response.json()["sku"]
    assert "reserved_quantity" not in response.json()["sku"]


def test_public_sku_batch_includes_status_for_blocked_products(
    client: TestClient,
    db_session: Session,
) -> None:
    visible_product = create_product_fixture(db_session, "Visible iPhone")
    visible_sku = create_sku_fixture(db_session, visible_product, active_quantity=0)
    blocked_product = create_product_fixture(
        db_session,
        "Blocked iPhone",
        status="BLOCKED",
    )
    blocked_sku = create_sku_fixture(db_session, blocked_product, active_quantity=5)

    response = client.post(
        "/api/v1/public/skus/batch",
        headers=SERVICE_HEADERS,
        json={"sku_ids": [visible_sku.id, blocked_sku.id]},
    )

    assert response.status_code == 200
    assert [item["sku"]["id"] for item in response.json()] == [
        visible_sku.id,
        blocked_sku.id,
    ]
    assert response.json()[1]["product"] == {
        "id": blocked_product.id,
        "title": "Blocked iPhone",
        "status": "BLOCKED",
        "deleted": False,
    }
    assert "seller_id" not in response.json()[1]["product"]
