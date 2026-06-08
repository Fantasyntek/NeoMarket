from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.models import (
    CharacteristicValue,
    Product,
    ProductFieldReport,
    ProductImage,
    SKU,
    SKUCharacteristicValue,
)


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
OTHER_SELLER_ID = "11111111-1111-1111-1111-111111111111"
BLOCKING_REASON_ID = "a7b8c9d0-1234-5678-ef01-890123456789"
NONEXISTENT_PRODUCT_ID = "00000000-0000-0000-0000-000000000000"


def create_product_fixture(
    db_session: Session,
    status: str = "MODERATED",
    seller_id: str = TEST_SELLER_ID,
    blocked: bool = False,
) -> Product:
    product = Product(
        seller_id=seller_id,
        category_id=TEST_CATEGORY_ID,
        title="iPhone 15 Pro Max",
        slug="iphone-15-pro-max",
        description="Flagship Apple smartphone",
        status=status,
        blocked=blocked,
    )
    product.images = [ProductImage(url="/s3/iphone15-front.jpg", ordering=0)]
    product.characteristics = [CharacteristicValue(name="Brand", value="Apple")]
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def create_sku_fixture(
    db_session: Session,
    product: Product,
    reserved_quantity: int = 2,
) -> SKU:
    sku = SKU(
        product_id=product.id,
        name="256GB Black",
        price=12999000,
        cost_price=9500000,
        discount=0,
        image="/s3/iphone15-black-256.jpg",
        active_quantity=10,
        reserved_quantity=reserved_quantity,
    )
    sku.characteristics = [
        SKUCharacteristicValue(name="Color", value="Black"),
        SKUCharacteristicValue(name="Storage", value="256GB"),
    ]
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


def add_blocking_details(db_session: Session, product: Product, sku: SKU) -> None:
    product.blocking_reason_id = BLOCKING_REASON_ID
    product.blocking_reason_title = "Description does not match product"
    product.moderator_comment = "Description and photos do not match"
    product.field_reports = [
        ProductFieldReport(
            field_name="description",
            sku_id=None,
            comment="Description mentions leather, but photos show synthetic material",
        ),
        ProductFieldReport(
            field_name="sku_image",
            sku_id=sku.id,
            comment="SKU photo does not match the selected color",
        ),
    ]
    db_session.commit()
    db_session.refresh(product)


def test_get_moderated_product_returns_full_payload(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session, status="MODERATED")
    sku = create_sku_fixture(db_session, product, reserved_quantity=2)

    response = client.get(f"/api/v1/products/{product.id}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == product.id
    assert body["title"] == "iPhone 15 Pro Max"
    assert body["description"] == "Flagship Apple smartphone"
    assert body["status"] == "MODERATED"
    assert body["deleted"] is False
    assert body["blocked"] is False
    assert body["blocking_reason"] is None
    assert body["field_reports"] == []
    assert body["category"] == {"id": TEST_CATEGORY_ID, "name": "iOS"}
    assert body["images"][0]["url"] == "/s3/iphone15-front.jpg"
    assert body["characteristics"] == [
        {
            "id": body["characteristics"][0]["id"],
            "name": "Brand",
            "value": "Apple",
        }
    ]
    assert body["skus"][0]["id"] == sku.id
    assert body["skus"][0]["cost_price"] == 9500000
    assert body["skus"][0]["reserved_quantity"] == 2
    assert body["skus"][0]["characteristics"][0]["name"] == "Color"


def test_get_blocked_product_returns_blocking_reason_and_field_reports(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(
        db_session,
        status="BLOCKED",
        blocked=True,
    )
    sku = create_sku_fixture(db_session, product, reserved_quantity=0)
    add_blocking_details(db_session, product, sku)

    response = client.get(f"/api/v1/products/{product.id}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "BLOCKED"
    assert body["blocked"] is True
    assert body["blocking_reason"] == {
        "id": BLOCKING_REASON_ID,
        "title": "Description does not match product",
        "comment": "Description and photos do not match",
    }
    assert len(body["field_reports"]) == 2
    assert body["field_reports"][0] == {
        "field_name": "description",
        "sku_id": None,
        "comment": "Description mentions leather, but photos show synthetic material",
    }
    assert body["field_reports"][1]["field_name"] == "sku_image"
    assert body["field_reports"][1]["sku_id"] == sku.id


def test_get_others_product_returns_404(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session, seller_id=OTHER_SELLER_ID)
    auth_headers = {
        "Authorization": f"Bearer {create_access_token({'seller_id': TEST_SELLER_ID})}"
    }

    response = client.get(f"/api/v1/products/{product.id}", headers=auth_headers)

    assert response.status_code == 404
    assert response.json() == {"code": "NOT_FOUND", "message": "Product not found"}


def test_get_nonexistent_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get(f"/api/v1/products/{NONEXISTENT_PRODUCT_ID}", headers=auth_headers)

    assert response.status_code == 404
    assert response.json() == {"code": "NOT_FOUND", "message": "Product not found"}


def test_public_product_detail_hides_seller_only_sku_fields(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session, status="MODERATED")
    create_sku_fixture(db_session, product, reserved_quantity=4)

    response = client.get(
        f"/api/v1/public/products/{product.id}",
        headers={"X-Service-Key": "dev-service-key"},
    )

    assert response.status_code == 200
    sku_payload = response.json()["skus"][0]
    assert "cost_price" not in sku_payload
    assert "reserved_quantity" not in sku_payload
