# NeoMarket

Backend services for NeoMarket recovery tasks.

## Run Locally

```bash
cd services/b2b
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

```bash
cd services/b2c
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8001
```

## Run Tests

```bash
cd services/b2b
pytest -q
```

```bash
cd services/b2c
pytest -q
```

## Run With Docker

```bash
docker compose -f infra/docker-compose.yml up --build
```

B2B will be available at `http://localhost:8000`.
B2C will be available at `http://localhost:8001`.

## US-B2B-01 ADR

For product characteristics I considered three options: a JSON field on `Product`, a separate `CharacteristicValue` table, and a full EAV schema. For the first implementation I chose a separate `CharacteristicValue` table because it keeps adding new characteristics simple while still allowing normal SQL filtering later. A JSON field is faster to ship, but filtering by individual characteristics becomes database-specific and harder to index consistently. A full EAV schema is more flexible, but it adds query and validation complexity too early for the current flow.

## US-B2B-02 ADR

For delivering the `CREATED` event to Moderation I considered a synchronous POST inside the request handler, an outbox pattern, and fire-and-forget background delivery. I chose a lightweight outbox pattern: the SKU, product status change, and event record are committed together, then the service immediately tries to dispatch the event with `X-Service-Key`. If Moderation is unavailable, the product state is not rolled back and the pending outbox row can be retried later. A plain synchronous POST is simpler but couples product creation to Moderation uptime, while fire-and-forget is also simple but can lose events on process crash.

## US-B2B-03 ADR

For IDOR protection while editing products and SKUs I considered explicit checks in each view, a reusable permission layer, and filtering all write queries by owner. I chose explicit ownership checks in the endpoint for this iteration: product edits compare `product.seller_id` with the JWT claim, and SKU edits check ownership through `sku.product.seller_id`. This is simple to maintain in the current small FastAPI service and keeps the authorization rule visible next to each state transition. A permission abstraction or owner-filtered repository would reduce the risk of forgetting the check as the API grows, but it adds indirection before the endpoint surface is stable.

## US-B2B-04 ADR

For the two cascade events on product deletion I considered two synchronous POST calls, an outbox for both events, and a mixed approach with synchronous Moderation delivery plus an outbox only for B2C. I chose outbox records for both Moderation and B2C because the `deleted=true` state and both event payloads are committed together before delivery. If either downstream service is unavailable, deletion is not rolled back and the failed event remains visible for retry instead of silently disappearing. This is slightly more code than two direct POST calls, but it gives clearer behavior for partial failures and keeps data consistency easier to reason about.

## US-B2B-05 ADR

For `GET /products/{id}` I considered one endpoint with an explicit auth-mode branch, two separate view functions, and a reusable permission class. I chose one endpoint with a small branch between seller JWT and `X-Service-Key` modes because it keeps the shared product lookup and response shape in one place. Seller mode includes `cost_price` and `reserved_quantity`, while service-key mode omits those seller-only SKU fields to reduce leakage risk. Two views would duplicate serialization logic, and a permission abstraction would be more useful once more endpoints need the same dual-mode access rule.

## US-B2B-06 ADR

For validating invoice items I considered checking SKU status in a serializer, in the endpoint view, or in model-level hooks. I chose endpoint-level validation because the rule depends on both the JWT seller and the related product status, so the code stays readable next to the invoice creation transaction. This keeps ownership and `MODERATED` checks hard to miss for the current API surface. A serializer would be cleaner in a DRF-style stack, while model hooks are easier to bypass or make too implicit when future admin and service flows create invoices differently.

## US-B2B-07 ADR

For separating seller-list and B2C catalog behavior I considered two different URLs, one URL with a branch by auth header, and two view functions selected by a router layer. I chose one `GET /products` endpoint with an explicit `X-Service-Key` branch because the canon uses the same URL and the mode boundary is easy to see at the top of the handler. The B2C branch uses a separate serializer that omits `cost_price` and `reserved_quantity`, which lowers the risk of leaking seller-only fields. Separate URLs would be clearer operationally, but they would drift from the canon contract and make adding shared filters more duplicative.

## US-B2B-08 ADR

For all-or-nothing reservation I considered a single transaction with `SELECT FOR UPDATE`, optimistic locking with retry, and a two-phase commit style flow. I chose one transaction with row locking because it is the most direct fit for decrementing multiple SKU counters together and keeps the rollback rule readable. It performs well for the expected contention pattern because only the touched SKU rows are locked, while optimistic retries would add more edge cases around repeated B2C checkout attempts. Two-phase commit is unnecessary here because the inventory mutation and idempotency record live in the same database.

## US-B2B-09 ADR

For moderation event idempotency I considered a separate `processed_events` table keyed by `idempotency_key`, storing the last event key on `Product`, and an upsert guarded by product/status conditions. I chose a separate processed moderation events table because the database primary key gives a simple uniqueness boundary and keeps duplicate detection independent of the product's current status. This has lower race-condition risk than checking mutable product fields and is easier to support as more moderation event types appear. A conditional upsert could be compact, but it would make the state transition rules harder to read and test.

## US-B2B-10 ADR

For fulfill idempotency I considered a separate `fulfilled_orders` table keyed by `order_id`, storing the last fulfilled order on each SKU, and relying on `reserved_quantity` checks. I chose a separate fulfill operations table because retries from B2C can be answered without touching SKU counters again, which directly reduces the risk of double deduction. A per-SKU field would not work cleanly for multi-SKU orders, and using only `reserved_quantity` cannot distinguish a duplicate retry from a new invalid request. The table approach is simple to support and matches the existing reserve/unreserve idempotency pattern.

## US-B2B-11 ADR

For `skus_count` and `total_active_quantity` in the seller product list I considered SQL aggregate annotations/subqueries, computing them in the serializer after loading products, and raw SQL. I chose aggregate subqueries in the list query because they avoid N+1 SKU loading while keeping the endpoint in normal SQLAlchemy code. Serializer-side calculation is simpler but can issue one SKU query per product as the list grows. Raw SQL would be efficient, but it is harder to maintain alongside the existing ORM filters and response logic.

## US-B2B-12 ADR

For ordering SKU deletion guardrails I considered separate checks with early returns, one `validate_deletion` function, and putting deletion checks into serializer-style validation. I chose explicit early-return checks inside the endpoint because the canonical order is business-critical: ownership, `HARD_BLOCKED`, then active reserves. A single validation function would reduce endpoint size, but it can hide the order of checks unless carefully documented. Serializer validation does not fit this service style well and increases the risk that future side effects are added after the wrong guardrail.

## US-CAT-01 ADR

For catalog facets I considered SQL `GROUP BY` on each request in the source catalog service, a TTL cache of facet responses, and denormalized counters in a separate table. I chose request-time calculation over the current B2B response for this first B2C iteration because B2C does not store products and this keeps facet counts consistent with the visible catalog payload returned by B2B. A TTL cache would reduce repeated load but can show stale counts after moderation or stock changes. Denormalized counters would scale better for a large catalog, but they add invalidation complexity before the event model for B2C catalog projections exists.

## US-CAT-02 ADR

For catalog search I considered SQL `LIKE`/`icontains`, PostgreSQL `pg_trgm`, and full-text `SearchVector`. I chose the `LIKE`/`icontains` style for the MVP because B2C proxies the `search` parameter to B2B and only needs lightweight length validation plus deterministic filtering for the current response shape. `pg_trgm` would improve typo tolerance and ranking, but it requires PostgreSQL-specific setup that is unnecessary for the first search flow. `SearchVector` can provide better relevance for large text fields, but it is more complex to tune and maintain before the catalog schema stabilizes.

## US-CAT-03 ADR

For separating B2B and B2C product representations I considered a dedicated public serializer, view-level field filtering, and a separate B2B endpoint returning only buyer-safe data. I chose a dedicated public serializer in B2C because it explicitly whitelists buyer-visible fields and prevents new internal B2B fields from leaking by default. View-level filtering is quicker but easier to miss when nested SKU fields change. A separate B2B endpoint would also reduce leakage risk, but it adds another cross-service contract before the product-card response shape is stable.

## US-CAT-04 ADR

For similar products I considered random selection from the same category, ranking by the highest characteristic overlap, and cached precomputed recommendations. I chose deterministic selection from the same category with a parent-category fallback because it is simple for the MVP and gives stable results across repeated requests. Random ordering is easy to ship but makes tests and user experience less predictable. Characteristic ranking and precomputed recommendations can improve relevance later, but they need more catalog data and invalidation logic than this first B2C integration has.

## US-CAT-05 ADR

For category hierarchy storage I considered PostgreSQL `ltree`, adjacency list with recursive traversal, and materialized path. I chose adjacency list for this service boundary because B2C receives a flat category list from B2B and can build trees and breadcrumbs without storing its own category projection. Breadcrumb lookup is fast enough for the small MVP tree after building an in-memory id index, and orphan detection is straightforward because every `parent_id` must exist in that same index. `ltree` and materialized path would speed up deep breadcrumb queries at scale, but they add database-specific storage decisions before B2C owns category persistence.

## B2B Public Catalog Contract

The seller cabinet and service-to-service catalog use separate routes. Seller requests use Bearer JWT on `/api/v1/products`, while B2C uses `X-Service-Key` on `/api/v1/public/products`, `/api/v1/public/products/batch`, and `/api/v1/public/products/{product_id}`. I considered keeping one route with an authorization-header branch, rewriting the path at ingress, and exposing a dedicated public router. The dedicated router matches the OpenAPI contract directly and makes seller-only fields such as `cost_price` and `reserved_quantity` unavailable by construction in public responses.

## B2B Moderation Event Contract

Moderation decisions are accepted at `POST /api/v1/moderation/events` with `X-Service-Key`. The request follows `ModerationEventRequest`: `event_type` selects `MODERATED` or `BLOCKED`, `occurred_at` is a required timezone-aware date-time, and blocking data is supplied through the flat `blocking_reason_id`, `moderator_comment`, and `field_reports` fields. Successful processing and idempotent duplicates return `204 No Content`.

## B2B Inventory Reserve Contract

Inventory reservation endpoints follow the B2B OpenAPI paths `POST /api/v1/inventory/reserve` and `POST /api/v1/inventory/unreserve`. Reserve requests require both `idempotency_key` and `order_id`, and return `{order_id, status, reserved_at}`; unreserve returns `{order_id, status, processed_at}`. The serialized response is stored with the idempotency operation so retries return the original timestamp without applying inventory changes again.

## B2B Inventory Fulfill Contract

Delivered orders are finalized through `POST /api/v1/inventory/fulfill`. A successful request returns `{order_id, status: "FULFILLED", processed_at}`, and the complete response is stored by `order_id`. Idempotent retries therefore return the original processing timestamp without decreasing `reserved_quantity` twice.

## B2C Catalog Product Contract

B2C product routes use the `/api/v1/catalog/products` namespace for listing, detail, and similar products. The list accepts `q`, OpenAPI sort values, and singular `filter[...]` deep-object parameters, while all product serializers emit the shared `CatalogProductCard` fields. Product details add buyer-safe SKU data with `available_quantity`, and similar products return a flat array as required by the contract.

## B2C Category Navigation Contract

Category navigation exposes a flat `GET /api/v1/catalog/categories` response and a separate nested `GET /api/v1/catalog/categories/tree` response. Both representations include the required `level` and root-to-node `path` fields. The hierarchy is validated for missing parents and cycles before either representation is returned.

## B2B Seller Product List Contract

Seller product list items include the required `slug` and `category_id` fields in addition to aggregate SKU counters. Soft-deleted products are excluded by default and are returned only when `include_deleted=true`, matching the B2B OpenAPI contract. Seller ownership continues to come exclusively from the JWT claim, so query parameters cannot widen the result set.

## B2B SKU Re-moderation

Adding a new SKU to a `MODERATED` or `BLOCKED` product is treated as a content edit: the product returns to `ON_MODERATION` and an `EDITED` event is recorded in the Moderation outbox. Adding another SKU while the product is already `ON_MODERATION` does not emit a duplicate event. The original first-SKU transition from `CREATED` continues to emit `CREATED`.

## B2B Outgoing Moderation Events

B2B sends product lifecycle events to `POST /api/v1/b2b/events` with `X-Service-Key`. The payload follows the Moderation OpenAPI contract: `event_type` is one of `PRODUCT_CREATED`, `PRODUCT_EDITED`, or `PRODUCT_DELETED`, `occurred_at` is a UTC timestamp, and event data is nested under `payload`. Created and edited events include the current product snapshot in `json_after`, while edited events also include the required `json_before` object. The exact transmitted document is persisted in the Moderation outbox for reliable inspection and retry support.

## US-CART-01 ADR

For identifying the owner of a favorites list I considered accepting `user_id` from query parameters, trusting an `X-User-Id` header, and reading the user identifier from verified JWT claims. I chose the JWT `sub` claim because it prevents clients from selecting another user's records and keeps the IDOR boundary inside the B2C service. Query parameters are ignored, while a direct `X-User-Id` header would only be safe behind a trusted gateway that strips client-supplied values. This approach adds a small amount of JWT validation code but makes ownership consistent across add, delete, and list operations.

## B2C Favorites

Favorites are available to authenticated buyers through `POST`, `DELETE`, and `GET /api/v1/favorites`. B2C persists only `user_id`, `product_id`, and `added_at`; product cards are batch-enriched from the B2B public catalog on every list request, so blocked or deleted products are excluded without deleting the stored favorite. Adding an existing favorite returns `200`, deleting a missing favorite returns `204`, and `user_id` always comes from the signed JWT `sub` claim.

## US-CART-02 ADR

For storing subscription event preferences I considered PostgreSQL `ArrayField`, a normalized child table with one row per event type, and a JSON array on the subscription record. I chose a JSON field because it works with the current SQLite-backed B2C service and adding a new notification type does not require a schema migration. A child table would make filtering by event type more explicit at scale, while `ArrayField` provides convenient PostgreSQL queries but would tie the MVP to one database. Validation at the API boundary keeps the stored JSON limited to the supported `IN_STOCK` and `PRICE_DOWN` values.

## B2C Product Subscriptions

Authenticated buyers can create and remove notification subscriptions through `POST` and `DELETE /api/v1/favorites/{product_id}/subscribe`. Subscription creation validates `notify_on`, verifies that the product is currently visible through B2B, and returns `409` when the same user is already subscribed to the product. Unsubscribe is idempotent and returns `204`, while notification delivery remains outside the current scope. Ownership always comes from the signed JWT `sub` claim.

## US-CART-03 ADR

For guest cart identity I considered an `X-Session-Id` header, an HTTP-only cookie, and a temporary signed JWT. I chose an opaque UUID in `X-Session-Id` because it works consistently for browsers and mobile clients without requiring cookie support, while authenticated ownership still comes only from the verified JWT `sub` claim. A plain session identifier can be copied if exposed, so all item queries are scoped by identity and foreign items return `404`; a temporary JWT would reduce tampering but adds token lifecycle complexity before guest authentication exists. When both identities are present, the guest cart is merged into the JWT user's cart using `MAX(quantity)`.

## B2C Cart

The cart stores only identity, product/SKU references, and quantity; current prices, stock, and `unavailable_reason` are calculated from B2B on every response. Guests use `X-Session-Id`, authenticated buyers use JWT `sub`, and a request carrying both automatically merges the guest cart into the user cart. Adding an existing SKU increments quantity without reserving inventory, unavailable lines remain visible with zero `line_total`, and only available lines are included in `checkout_payload`. B2B exposes service-key protected SKU lookup and batch endpoints so out-of-stock variants can be enriched without exposing seller-only fields.

## US-CART-04 ADR

For CTR analytics I considered inserting one relational row per request, accepting event batches and inserting them in one transaction, and forwarding events to an external analytics platform. I chose batched relational inserts because they reduce database round trips for high-volume impressions while preserving straightforward SQL aggregation of clicks and impressions per banner. A dedicated analytics system would scale further but adds infrastructure outside the current MVP, while one request per event creates unnecessary write overhead. The endpoint validates the complete batch before committing, so unknown banners cannot produce partial analytics data.

## B2C Home Banners

The public `GET /api/v1/home/banners` endpoint returns only enabled banners within their optional schedule, ordered by ascending priority. `POST /api/v1/banner-events` accepts impression and click events in batches and stores them for CTR aggregation; it does not require buyer authentication. The legacy OpenAPI route `GET /api/v1/catalog/banners` is retained as a flat-array alias, while the canonical home endpoint returns `{items, total_count}`. Banner creation remains an administrative responsibility and is not exposed through the public API.

## US-CART-05 ADR

For collection membership I considered storing a UUID array on each collection, using a separate ordered association table, and copying product data into B2C. I chose an association table because content managers can reorder or update individual products without rewriting an entire array, while B2C still stores only product UUIDs. Copying product fields would become stale when B2B changes or deletes a product, and an embedded array is harder to query and constrain as collections grow. Missing UUIDs from the B2B batch response are reported through `unavailable_ids`, preserving consistency without breaking the collection.

## B2C Product Collections

The public `GET /api/v1/main/collections` endpoint returns active collection metadata without nested products. `GET /api/v1/collections/{collection_id}/products` reads ordered product UUIDs, enriches the requested page through the B2B public batch API, and returns unavailable UUIDs separately. Collections remain valid when every product is unavailable, and B2C never stores a product snapshot. The legacy OpenAPI route `GET /api/v1/catalog/collections` remains available as a flat metadata alias.
