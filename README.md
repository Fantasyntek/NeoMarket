# NeoMarket

Backend services for NeoMarket recovery tasks.

## Run Locally

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Run Tests

```bash
pytest -q
```

## US-B2B-01 ADR

For product characteristics I considered three options: a JSON field on `Product`, a separate `CharacteristicValue` table, and a full EAV schema. For the first implementation I chose a separate `CharacteristicValue` table because it keeps adding new characteristics simple while still allowing normal SQL filtering later. A JSON field is faster to ship, but filtering by individual characteristics becomes database-specific and harder to index consistently. A full EAV schema is more flexible, but it adds query and validation complexity too early for the current flow.

## US-B2B-02 ADR

For delivering the `CREATED` event to Moderation I considered a synchronous POST inside the request handler, an outbox pattern, and fire-and-forget background delivery. I chose a lightweight outbox pattern: the SKU, product status change, and event record are committed together, then the service immediately tries to dispatch the event with `X-Service-Key`. If Moderation is unavailable, the product state is not rolled back and the pending outbox row can be retried later. A plain synchronous POST is simpler but couples product creation to Moderation uptime, while fire-and-forget is also simple but can lose events on process crash.
