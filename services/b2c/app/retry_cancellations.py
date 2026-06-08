from app.b2b_client import HttpB2BClient
from app.cancellation_retry import process_cancellation_retries
from app.database import SessionLocal, init_db


def main() -> None:
    init_db()
    with SessionLocal() as db:
        completed = process_cancellation_retries(db, HttpB2BClient())
    print(f"Completed cancellation retries: {completed}")


if __name__ == "__main__":
    main()
