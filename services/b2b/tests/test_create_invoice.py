from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.models import Invoice, Product, SKU


TEST_CATEGORY_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TEST_SELLER_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
OTHER_SELLER_ID = "11111111-1111-1111-1111-111111111111"


def create_product_fixture(
    db_session: Session,
    status: str = "MODERATED",
    seller_id: str = TEST_SELLER_ID,
) -> Product:
    product = Product(
        seller_id=seller_id,
        category_id=TEST_CATEGORY_ID,
        title="iPhone 15 Pro Max",
        slug="iphone-15-pro-max",
        description="Flagship Apple smartphone",
        status=status,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def create_sku_fixture(
    db_session: Session,
    product: Product,
    name: str = "256GB Black",
) -> SKU:
    sku = SKU(
        product_id=product.id,
        name=name,
        price=12999000,
        cost_price=9500000,
        discount=0,
        image="/s3/iphone15-black-256.jpg",
        active_quantity=0,
        reserved_quantity=0,
    )
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


def test_create_invoice_with_moderated_sku_returns_201(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session, status="MODERATED")
    sku = create_sku_fixture(db_session, product)

    response = client.post(
        "/api/v1/invoices",
        json={"items": [{"sku_id": sku.id, "quantity": 10}]},
        headers=auth_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["seller_id"] == TEST_SELLER_ID
    assert body["status"] == "PENDING"
    assert body["items"] == [
        {
            "id": body["items"][0]["id"],
            "sku_id": sku.id,
            "sku_name": "256GB Black",
            "quantity": 10,
            "accepted_quantity": None,
        }
    ]
    invoice = db_session.query(Invoice).one()
    assert invoice.status == "PENDING"
    assert invoice.items[0].accepted_quantity is None


def test_empty_items_returns_400(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/invoices",
        json={"items": []},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "At least one item is required",
    }


def test_non_moderated_sku_returns_400(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    product = create_product_fixture(db_session, status="ON_MODERATION")
    sku = create_sku_fixture(db_session, product)

    response = client.post(
        "/api/v1/invoices",
        json={"items": [{"sku_id": sku.id, "quantity": 10}]},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "Invoice can only be created for MODERATED products",
    }
    assert db_session.query(Invoice).count() == 0


def test_others_sku_returns_403(
    client: TestClient, db_session: Session
) -> None:
    product = create_product_fixture(
        db_session,
        status="MODERATED",
        seller_id=OTHER_SELLER_ID,
    )
    sku = create_sku_fixture(db_session, product)
    auth_headers = {
        "Authorization": f"Bearer {create_access_token({'seller_id': TEST_SELLER_ID})}"
    }

    response = client.post(
        "/api/v1/invoices",
        json={"items": [{"sku_id": sku.id, "quantity": 10}]},
        headers=auth_headers,
    )

    assert response.status_code == 403
    assert response.json() == {
        "code": "NOT_OWNER",
        "message": "One or more SKUs do not belong to the authenticated seller",
    }
    assert db_session.query(Invoice).count() == 0
