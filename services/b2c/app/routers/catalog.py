from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.b2b_client import (
    B2BClient,
    B2BResponseError,
    B2BUnavailableError,
    get_b2b_client,
)
from app.errors import api_error


router = APIRouter(prefix="/api/v1", tags=["Catalog"])

ALLOWED_SORTS = {
    "rating",
    "popularity",
    "price_asc",
    "price_desc",
    "date_desc",
    "discount_desc",
}
DEFAULT_SORT = "rating"
FILTER_PREFIXES = ("filters[", "filter[")
FACET_NAME_ALIASES = {
    "brand": {"brand", "бренд"},
    "color": {"color", "цвет"},
    "memory": {"memory", "storage", "объем памяти", "объём памяти", "память"},
}


def _invalid_sort_error() -> None:
    allowed = ", ".join(
        ["rating", "popularity", "price_asc", "price_desc", "date_desc", "discount_desc"]
    )
    raise api_error(
        400,
        "INVALID_REQUEST",
        f"Invalid sort parameter. Allowed: {allowed}",
    )


def _validate_search_query(search: str | None) -> str | None:
    if search is None:
        return None
    normalized_search = search.strip()
    if len(normalized_search) < 3:
        raise api_error(
            400,
            "INVALID_REQUEST",
            "Search query must be at least 3 characters",
        )
    if len(normalized_search) > 255:
        raise api_error(
            400,
            "INVALID_REQUEST",
            "Search query must be at most 255 characters",
        )
    return normalized_search


def _parse_filters(request: Request) -> dict[str, str]:
    filters: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        for prefix in FILTER_PREFIXES:
            if key.startswith(prefix) and key.endswith("]"):
                filters[key[len(prefix) : -1]] = value
    return filters


def _fetch_visible_products(
    b2b_client: B2BClient,
    category_id: str | None,
    search: str | None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": 100, "offset": 0, "sort": "date_desc"}
    if category_id:
        params["category"] = category_id
    if search:
        params["search"] = search

    try:
        response = b2b_client.list_products(params)
    except B2BResponseError as exc:
        payload = exc.payload
        raise api_error(
            exc.status_code,
            str(payload.get("code", "B2B_ERROR")),
            str(payload.get("message", "B2B request failed")),
        )
    except B2BUnavailableError:
        raise api_error(502, "B2B_UNAVAILABLE", "Catalog is temporarily unavailable")

    return list(response.get("items", []))


def _normalize_name(value: str) -> str:
    return value.strip().lower().replace("_", " ").replace("-", " ")


def _characteristic_matches(filter_name: str, characteristic_name: str) -> bool:
    normalized_filter = _normalize_name(filter_name)
    normalized_characteristic = _normalize_name(characteristic_name)
    aliases = FACET_NAME_ALIASES.get(normalized_filter, {normalized_filter})
    return normalized_characteristic in aliases


def _iter_characteristics(product: dict[str, Any]) -> list[dict[str, Any]]:
    characteristics = list(product.get("characteristics", []))
    for sku in product.get("skus", []):
        characteristics.extend(sku.get("characteristics", []))
    return characteristics


def _product_matches_filter(product: dict[str, Any], name: str, value: str) -> bool:
    if name == "price_min":
        return _product_price(product) >= int(value)
    if name == "price_max":
        return _product_price(product) <= int(value)

    for characteristic in _iter_characteristics(product):
        if not _characteristic_matches(name, str(characteristic.get("name", ""))):
            continue
        if str(characteristic.get("value", "")).lower() == value.lower():
            return True
    return False


def _apply_filters(
    products: list[dict[str, Any]],
    category_id: str | None,
    filters: dict[str, str],
) -> list[dict[str, Any]]:
    filtered_products = products
    if category_id:
        filtered_products = [
            product
            for product in filtered_products
            if _product_category_id(product) == category_id
        ]
    for name, value in filters.items():
        filtered_products = [
            product
            for product in filtered_products
            if _product_matches_filter(product, name, value)
        ]
    return filtered_products


def _apply_search(
    products: list[dict[str, Any]],
    search: str | None,
) -> list[dict[str, Any]]:
    if search is None:
        return products

    needle = search.lower()
    return [
        product
        for product in products
        if needle in str(product.get("title", "")).lower()
        or needle in str(product.get("description", "")).lower()
    ]


def _product_category_id(product: dict[str, Any]) -> str | None:
    category = product.get("category")
    if isinstance(category, dict):
        return category.get("id")
    return product.get("category_id")


def _product_parent_category_id(product: dict[str, Any]) -> str | None:
    category = product.get("category")
    if isinstance(category, dict):
        parent = category.get("parent")
        if isinstance(parent, dict):
            return parent.get("id")
        parent_id = category.get("parent_id")
        if isinstance(parent_id, str):
            return parent_id
    parent_category_id = product.get("parent_category_id")
    return parent_category_id if isinstance(parent_category_id, str) else None


def _product_price(product: dict[str, Any]) -> int:
    prices = []
    for sku in product.get("skus", []):
        if sku.get("active_quantity", 0) <= 0:
            continue
        price = int(sku.get("price", 0))
        discount = int(sku.get("discount", 0))
        prices.append(max(price - discount, 0))
    return min(prices) if prices else 0


def _product_max_discount(product: dict[str, Any]) -> int:
    discounts = [
        int(sku.get("discount", 0))
        for sku in product.get("skus", [])
        if sku.get("active_quantity", 0) > 0
    ]
    return max(discounts) if discounts else 0


def _product_image(product: dict[str, Any]) -> str | None:
    images = product.get("images", [])
    if images:
        return images[0].get("url")
    skus = product.get("skus", [])
    if skus:
        return skus[0].get("image")
    return None


def _sort_products(products: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    if sort == "price_asc":
        return sorted(products, key=lambda product: (_product_price(product), product["id"]))
    if sort == "price_desc":
        return sorted(
            products,
            key=lambda product: (_product_price(product), product["id"]),
            reverse=True,
        )
    if sort == "discount_desc":
        return sorted(
            products,
            key=lambda product: (_product_max_discount(product), product["id"]),
            reverse=True,
        )
    if sort == "date_desc":
        return sorted(products, key=lambda product: product.get("created_at", ""), reverse=True)
    return sorted(products, key=lambda product: product["id"])


def _serialize_product_card(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": product["id"],
        "title": product["title"],
        "image": _product_image(product),
        "price": _product_price(product),
        "in_stock": any(sku.get("active_quantity", 0) > 0 for sku in product.get("skus", [])),
        "is_in_cart": False,
    }


def _public_sku(sku: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": sku["id"],
        "name": sku.get("name"),
        "price": int(sku.get("price", 0)),
        "discount": int(sku.get("discount", 0)),
        "image": sku.get("image"),
        "active_quantity": int(sku.get("active_quantity", 0)),
        "in_stock": int(sku.get("active_quantity", 0)) > 0,
        "characteristics": [
            {
                "name": characteristic.get("name"),
                "value": characteristic.get("value"),
            }
            for characteristic in sku.get("characteristics", [])
        ],
    }


def _serialize_product_detail(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": product["id"],
        "slug": product.get("slug"),
        "title": product["title"],
        "description": product.get("description", ""),
        "images": [
            {"url": image.get("url"), "ordering": image.get("ordering", 0)}
            for image in product.get("images", [])
        ],
        "status": product.get("status"),
        "characteristics": [
            {
                "name": characteristic.get("name"),
                "value": characteristic.get("value"),
            }
            for characteristic in product.get("characteristics", [])
        ],
        "skus": [_public_sku(sku) for sku in product.get("skus", [])],
    }


def _product_is_customer_visible(product: dict[str, Any]) -> bool:
    return product.get("status") == "MODERATED" and product.get("deleted") is not True


def _raise_b2b_response_error(exc: B2BResponseError) -> None:
    payload = exc.payload
    raise api_error(
        exc.status_code,
        str(payload.get("code", "B2B_ERROR")),
        str(payload.get("message", "B2B request failed")),
    )


def _fetch_product_detail(b2b_client: B2BClient, product_id: str) -> dict[str, Any]:
    try:
        return b2b_client.get_product(product_id)
    except B2BResponseError as exc:
        _raise_b2b_response_error(exc)
    except B2BUnavailableError:
        raise api_error(502, "B2B_UNAVAILABLE", "Product is temporarily unavailable")


def _select_similar_products(
    current_product: dict[str, Any],
    products: list[dict[str, Any]],
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    current_product_id = current_product["id"]
    candidates = [
        product
        for product in products
        if product.get("id") != current_product_id
        and _product_is_customer_visible(product)
        and any(sku.get("active_quantity", 0) > 0 for sku in product.get("skus", []))
    ]
    candidates = sorted(candidates, key=lambda product: product["id"])
    return candidates[offset : offset + limit], len(candidates)


def _build_facets(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for product in products:
        seen_values: set[tuple[str, str]] = set()
        for characteristic in _iter_characteristics(product):
            name = str(characteristic.get("name", "")).strip()
            value = str(characteristic.get("value", "")).strip()
            if not name or not value or (name, value) in seen_values:
                continue
            seen_values.add((name, value))
            counts[name][value] += 1

    return [
        {
            "name": name,
            "values": [
                {"value": value, "count": count}
                for value, count in sorted(values.items())
            ],
        }
        for name, values in sorted(counts.items())
    ]


@router.get("/products")
def list_products(
    request: Request,
    b2b_client: B2BClient = Depends(get_b2b_client),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    category_id: str | None = None,
    sort: str = DEFAULT_SORT,
    search: str | None = None,
) -> dict[str, Any]:
    if sort not in ALLOWED_SORTS:
        _invalid_sort_error()

    normalized_search = _validate_search_query(search)
    filters = _parse_filters(request)
    products = _fetch_visible_products(b2b_client, category_id, normalized_search)
    searched_products = _apply_search(products, normalized_search)
    filtered_products = _apply_filters(searched_products, category_id, filters)
    sorted_products = _sort_products(filtered_products, sort)
    page = sorted_products[offset : offset + limit]

    return {
        "items": [_serialize_product_card(product) for product in page],
        "total_count": len(sorted_products),
        "limit": limit,
        "offset": offset,
    }


@router.get("/products/{product_id}")
def get_product_card(
    product_id: str,
    b2b_client: B2BClient = Depends(get_b2b_client),
) -> dict[str, Any]:
    product = _fetch_product_detail(b2b_client, product_id)

    if not _product_is_customer_visible(product):
        raise api_error(404, "NOT_FOUND", "Product not found")

    return _serialize_product_detail(product)


@router.get("/products/{product_id}/similar")
def get_similar_products(
    product_id: str,
    b2b_client: B2BClient = Depends(get_b2b_client),
    category: str | None = None,
    limit: int = Query(default=8, ge=1, le=20),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    current_product = _fetch_product_detail(b2b_client, product_id)
    if not _product_is_customer_visible(current_product):
        raise api_error(404, "NOT_FOUND", "Product not found")

    category_id = category or _product_category_id(current_product)
    if category_id is None:
        return {"items": [], "total_count": 0, "limit": limit, "offset": offset}

    products = _fetch_visible_products(b2b_client, category_id, search=None)
    selected_products, total_count = _select_similar_products(
        current_product,
        products,
        limit,
        offset,
    )

    if len(selected_products) < limit:
        parent_category_id = _product_parent_category_id(current_product)
        if parent_category_id is not None and parent_category_id != category_id:
            parent_products = _fetch_visible_products(
                b2b_client,
                parent_category_id,
                search=None,
            )
            existing_ids = {product["id"] for product in selected_products}
            parent_candidates = [
                product
                for product in parent_products
                if product.get("id") != current_product["id"]
                and product["id"] not in existing_ids
                and _product_category_id(product) != category_id
                and _product_is_customer_visible(product)
                and any(sku.get("active_quantity", 0) > 0 for sku in product.get("skus", []))
            ]
            parent_candidates = sorted(parent_candidates, key=lambda product: product["id"])
            missing_count = limit - len(selected_products)
            selected_products.extend(parent_candidates[:missing_count])
            total_count += len(parent_candidates)

    return {
        "items": [_serialize_product_card(product) for product in selected_products],
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }


@router.get("/catalog/facets")
def get_facets(
    request: Request,
    b2b_client: B2BClient = Depends(get_b2b_client),
    category_id: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    normalized_search = _validate_search_query(search)
    filters = _parse_filters(request)
    products = _fetch_visible_products(b2b_client, category_id, normalized_search)
    searched_products = _apply_search(products, normalized_search)
    filtered_products = _apply_filters(searched_products, category_id, filters)

    return {
        "category_id": category_id,
        "facets": _build_facets(filtered_products),
    }
