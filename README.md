# NeoMarket

Backend services for NeoMarket recovery tasks.

## Structure

```text
services/
  b2b/          # Seller cabinet service
infra/
  docker-compose.yml
```

## Run B2B Locally

```bash
cd services/b2b
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Run B2B Tests

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
