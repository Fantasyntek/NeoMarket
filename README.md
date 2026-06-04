# NeoMarket

Backend services for NeoMarket recovery tasks.

## Run Locally

```bash
cd services/b2b
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Run Tests

```bash
cd services/b2b
pytest -q
```

## Run With Docker

```bash
docker compose -f infra/docker-compose.yml up --build
```

B2B will be available at `http://localhost:8000`.

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
