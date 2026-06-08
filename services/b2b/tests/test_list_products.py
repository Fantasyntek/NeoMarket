from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.models import Product, ProductImage, SKU


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
OTHER_SELLER_ID = "11111111-1111-1111-1111-111111111111"


def create_product_fixture(
    db_session: Session,
    title: str,
    seller_id: str = TEST_SELLER_ID,
    status: str = "CREATED",
    deleted: bool = False,
) -> Product:
    product = Product(
        seller_id=seller_id,
        category_id=TEST_CATEGORY_ID,
        title=title,
        slug=title.lower().replace(" ", "-"),
        description=f"{title} description",
        status=status,
        deleted=deleted,
    )
    product.images = [ProductImage(url=f"/s3/{product.slug}.jpg", ordering=0)]
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def create_sku_fixture(
    db_session: Session,
    product: Product,
    active_quantity: int,
) -> SKU:
    sku = SKU(
        product_id=product.id,
        name="256GB Black",
        price=12999000,
        cost_price=9500000,
        discount=0,
        image="/s3/iphone15-black-256.jpg",
        active_quantity=active_quantity,
        reserved_quantity=1,
    )
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


def seller_headers(seller_id: str) -> dict[str, str]:
    token = create_access_token({"seller_id": seller_id})
    return {"Authorization": f"Bearer {token}"}


def test_list_returns_only_own_products(
    client: TestClient, db_session: Session
) -> None:
    own_product = create_product_fixture(db_session, "Own iPhone")
    create_sku_fixture(db_session, own_product, active_quantity=4)
    create_sku_fixture(db_session, own_product, active_quantity=6)
    create_product_fixture(db_session, "Competitor iPhone", seller_id=OTHER_SELLER_ID)

    response = client.get("/api/v1/products", headers=seller_headers(TEST_SELLER_ID))

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [own_product.id]
    assert body["items"][0]["slug"] == "own-iphone"
    assert body["items"][0]["category_id"] == TEST_CATEGORY_ID
    assert body["items"][0]["skus_count"] == 2
    assert body["items"][0]["total_active_quantity"] == 10
    assert body["total_count"] == 1


def test_idor_query_param_seller_id_ignored(
    client: TestClient, db_session: Session
) -> None:
    own_product = create_product_fixture(db_session, "Own iPhone")
    competitor_product = create_product_fixture(
        db_session,
        "Competitor iPhone",
        seller_id=OTHER_SELLER_ID,
    )

    response = client.get(
        f"/api/v1/products?seller_id={OTHER_SELLER_ID}",
        headers=seller_headers(TEST_SELLER_ID),
    )

    assert response.status_code == 200
    item_ids = [item["id"] for item in response.json()["items"]]
    assert own_product.id in item_ids
    assert competitor_product.id not in item_ids


def test_deleted_products_visible_with_deleted_flag(
    client: TestClient, db_session: Session
) -> None:
    deleted_product = create_product_fixture(
        db_session,
        "Deleted iPhone",
        deleted=True,
    )

    response = client.get(
        "/api/v1/products?include_deleted=true",
        headers=seller_headers(TEST_SELLER_ID),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["id"] == deleted_product.id
    assert body["items"][0]["deleted"] is True
    assert body["total_count"] == 1


def test_deleted_products_hidden_by_default(
    client: TestClient, db_session: Session
) -> None:
    create_product_fixture(
        db_session,
        "Deleted iPhone",
        deleted=True,
    )

    response = client.get("/api/v1/products", headers=seller_headers(TEST_SELLER_ID))

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total_count"] == 0


def test_status_filter_works_correctly(
    client: TestClient, db_session: Session
) -> None:
    blocked_product = create_product_fixture(
        db_session,
        "Blocked iPhone",
        status="BLOCKED",
    )
    create_product_fixture(db_session, "Moderated iPhone", status="MODERATED")

    response = client.get(
        "/api/v1/products?status=BLOCKED",
        headers=seller_headers(TEST_SELLER_ID),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [blocked_product.id]
    assert body["items"][0]["status"] == "BLOCKED"
    assert body["total_count"] == 1


def test_search_by_title_case_insensitive(
    client: TestClient, db_session: Session
) -> None:
    matching_product = create_product_fixture(db_session, "iPhone Titanium")
    create_product_fixture(db_session, "Android Fold")

    response = client.get(
        "/api/v1/products?search=iphone",
        headers=seller_headers(TEST_SELLER_ID),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [matching_product.id]
    assert body["total_count"] == 1
