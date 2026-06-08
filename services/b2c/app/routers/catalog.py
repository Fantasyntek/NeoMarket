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
    params: dict[str, Any] = {"limit": 100, "offset": 0, "sort": "created_desc"}
    if category_id:
        params["category_id"] = category_id
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


def _fetch_categories(b2b_client: B2BClient) -> list[dict[str, Any]]:
    try:
        response = b2b_client.list_categories()
    except B2BResponseError as exc:
        _raise_b2b_response_error(exc)
    except B2BUnavailableError:
        raise api_error(502, "B2B_UNAVAILABLE", "Categories are temporarily unavailable")

    if isinstance(response, list):
        return response
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


def _category_parent_id(category: dict[str, Any]) -> str | None:
    parent_id = category.get("parent_id")
    return parent_id if isinstance(parent_id, str) and parent_id else None


def _category_slug(category: dict[str, Any]) -> str:
    slug = category.get("slug")
    if isinstance(slug, str) and slug:
        return slug
    return str(category.get("name", "")).strip().lower().replace(" ", "-")


def _category_url(category: dict[str, Any]) -> str:
    slug = _category_slug(category)
    return f"/catalog/{slug}" if slug else f"/catalog/{category['id']}"


def _categories_by_id(categories: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {category["id"]: category for category in categories}


def _validate_category_hierarchy(categories: list[dict[str, Any]]) -> None:
    categories_by_id = _categories_by_id(categories)
    for category in categories:
        parent_id = _category_parent_id(category)
        if parent_id is not None and parent_id not in categories_by_id:
            raise api_error(
                422,
                "ORPHAN_NODE",
                "category hierarchy is broken",
            )

    for category in categories:
        seen_ids: set[str] = set()
        current = category
        while _category_parent_id(current) is not None:
            current_id = current["id"]
            if current_id in seen_ids:
                raise api_error(
                    422,
                    "ORPHAN_NODE",
                    "category hierarchy is broken",
                )
            seen_ids.add(current_id)
            current = categories_by_id[_category_parent_id(current)]


def _category_path(
    category_id: str,
    categories_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if category_id not in categories_by_id:
        raise api_error(404, "NOT_FOUND", "Category not found")

    path = []
    current = categories_by_id[category_id]
    while True:
        path.append(current)
        parent_id = _category_parent_id(current)
        if parent_id is None:
            break
        current = categories_by_id[parent_id]
    return list(reversed(path))


def _serialize_category_node(
    category: dict[str, Any],
    children_by_parent: dict[str | None, list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "id": category["id"],
        "name": category["name"],
        "parent_id": _category_parent_id(category),
        "children": [
            _serialize_category_node(child, children_by_parent)
            for child in children_by_parent.get(category["id"], [])
        ],
    }


def _build_category_tree(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    children_by_parent: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for category in categories:
        children_by_parent[_category_parent_id(category)].append(category)
    for children in children_by_parent.values():
        children.sort(key=lambda category: (str(category.get("name", "")), category["id"]))
    return [
        _serialize_category_node(category, children_by_parent)
        for category in children_by_parent.get(None, [])
    ]


def _serialize_category_detail(
    category: dict[str, Any],
    categories_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    parent_id = _category_parent_id(category)
    parent = categories_by_id.get(parent_id) if parent_id else None
    return {
        "id": category["id"],
        "name": category["name"],
        "slug": _category_slug(category),
        "description": category.get("description"),
        "parent": (
            {
                "id": parent["id"],
                "name": parent["name"],
                "slug": _category_slug(parent),
            }
            if parent is not None
            else None
        ),
        "product_count": int(category.get("product_count", 0)),
        "seo": category.get("seo", {}),
        "meta_tags": category.get("meta_tags", {}),
        "image_url": category.get("image_url"),
        "is_active": category.get("is_active", True),
        "created_at": category.get("created_at"),
        "updated_at": category.get("updated_at"),
    }


def _serialize_breadcrumbs(
    path: list[dict[str, Any]],
    category_id: str,
) -> list[dict[str, Any]]:
    return [
        {
            "id": category["id"],
            "slug": _category_slug(category),
            "name": category["name"],
            "url": _category_url(category),
            "level": index,
            "is_current": category["id"] == category_id,
        }
        for index, category in enumerate(path)
    ]


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


@router.get("/categories")
def get_category_tree(
    b2b_client: B2BClient = Depends(get_b2b_client),
) -> dict[str, Any]:
    categories = _fetch_categories(b2b_client)
    _validate_category_hierarchy(categories)
    return {"items": _build_category_tree(categories)}


@router.get("/categories/{category_id}")
def get_category_detail(
    category_id: str,
    b2b_client: B2BClient = Depends(get_b2b_client),
    include_product_count: bool = True,
) -> dict[str, Any]:
    categories = _fetch_categories(b2b_client)
    _validate_category_hierarchy(categories)
    categories_by_id = _categories_by_id(categories)
    if category_id not in categories_by_id:
        raise api_error(404, "NOT_FOUND", "Category not found")
    return _serialize_category_detail(categories_by_id[category_id], categories_by_id)


@router.get("/breadcrumbs")
def get_breadcrumbs(
    b2b_client: B2BClient = Depends(get_b2b_client),
    category_id: str | None = None,
    product_id: str | None = None,
) -> dict[str, Any]:
    if (category_id is None and product_id is None) or (
        category_id is not None and product_id is not None
    ):
        raise api_error(
            400,
            "INVALID_REQUEST",
            "only one of category_id or product_id must be provided",
        )

    resolved_category_id = category_id
    if product_id is not None:
        product = _fetch_product_detail(b2b_client, product_id)
        resolved_category_id = _product_category_id(product)
        if resolved_category_id is None:
            raise api_error(404, "NOT_FOUND", "Category not found")

    categories = _fetch_categories(b2b_client)
    _validate_category_hierarchy(categories)
    categories_by_id = _categories_by_id(categories)
    path = _category_path(str(resolved_category_id), categories_by_id)
    return {
        "data": _serialize_breadcrumbs(path, str(resolved_category_id)),
        "meta": {
            "resolved_via": "product_id" if product_id is not None else "category_id",
            "category_id": resolved_category_id,
        },
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
