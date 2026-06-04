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
