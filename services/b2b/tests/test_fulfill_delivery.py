from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import FulfillOperation, Product, SKU


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
SERVICE_HEADERS = {"X-Service-Key": "dev-service-key"}
ORDER_ID = "66666666-7777-8888-9999-000000000000"


def create_product_fixture(db_session: Session) -> Product:
    product = Product(
        seller_id=TEST_SELLER_ID,
        category_id=TEST_CATEGORY_ID,
        title="iPhone 15 Pro Max",
        slug="iphone-15-pro-max",
        description="Flagship Apple smartphone",
        status="MODERATED",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def create_sku_fixture(
    db_session: Session,
    product: Product,
    active_quantity: int = 3,
    reserved_quantity: int = 5,
) -> SKU:
    sku = SKU(
        product_id=product.id,
        name="256GB Black",
        price=12999000,
        cost_price=9500000,
        discount=0,
        image="/s3/iphone15-black-256.jpg",
        active_quantity=active_quantity,
        reserved_quantity=reserved_quantity,
    )
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


def fulfill_payload(sku: SKU, quantity: int = 2, order_id: str = ORDER_ID) -> dict:
    return {"order_id": order_id, "items": [{"sku_id": sku.id, "quantity": quantity}]}


def test_fulfill_decreases_reserved_quantity(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session)
    sku = create_sku_fixture(db_session, product, reserved_quantity=5)

    response = client.post(
        "/api/v1/inventory/fulfill",
        json=fulfill_payload(sku, quantity=2),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(sku)
    assert response.status_code == 200
    assert response.json()["order_id"] == ORDER_ID
    assert response.json()["status"] == "FULFILLED"
    assert response.json()["processed_at"].endswith("Z")
    assert sku.reserved_quantity == 3
    assert db_session.query(FulfillOperation).one().order_id == ORDER_ID


def test_active_quantity_unchanged(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session)
    sku = create_sku_fixture(db_session, product, active_quantity=7, reserved_quantity=5)

    response = client.post(
        "/api/v1/inventory/fulfill",
        json=fulfill_payload(sku, quantity=2),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(sku)
    assert response.status_code == 200
    assert sku.active_quantity == 7


def test_idempotent_fulfill_no_double_deduction(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session)
    sku = create_sku_fixture(db_session, product, reserved_quantity=5)

    first_response = client.post(
        "/api/v1/inventory/fulfill",
        json=fulfill_payload(sku, quantity=2),
        headers=SERVICE_HEADERS,
    )
    second_response = client.post(
        "/api/v1/inventory/fulfill",
        json=fulfill_payload(sku, quantity=2),
        headers=SERVICE_HEADERS,
    )

    db_session.refresh(sku)
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()
    assert sku.reserved_quantity == 3


def test_missing_service_key_returns_401(client: TestClient, db_session: Session) -> None:
    product = create_product_fixture(db_session)
    sku = create_sku_fixture(db_session, product, reserved_quantity=5)

    response = client.post(
        "/api/v1/inventory/fulfill",
        json=fulfill_payload(sku, quantity=2),
    )

    assert response.status_code == 401
    assert response.json() == {"code": "UNAUTHORIZED", "message": "Invalid service key"}


def test_legacy_fulfill_path_returns_404(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(db_session)
    sku = create_sku_fixture(db_session, product)

    response = client.post(
        "/api/v1/fulfill",
        json=fulfill_payload(sku),
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 404
