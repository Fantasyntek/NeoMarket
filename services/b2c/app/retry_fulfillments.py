from app.b2b_client import HttpB2BClient
from app.database import SessionLocal, init_db
from app.order_fulfillment import process_fulfillment_retries


def main() -> None:
    init_db()
    with SessionLocal() as db:
        completed = process_fulfillment_retries(db, HttpB2BClient())
    print(f"Completed fulfillment retries: {completed}")


if __name__ == "__main__":
    main()
