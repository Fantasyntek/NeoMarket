from collections.abc import Generator
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./neomarket-b2c.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _upgrade_orders_schema()


def _upgrade_orders_schema() -> None:
    inspector = inspect(engine)
    if "orders" not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("orders")
    }
    missing_columns = {
        "address_id": "VARCHAR(36)",
        "payment_method_id": "VARCHAR(36)",
        "comment": "TEXT",
    }
    with engine.begin() as connection:
        for column_name, column_type in missing_columns.items():
            if column_name not in existing_columns:
                connection.execute(
                    text(
                        f"ALTER TABLE orders ADD COLUMN {column_name} {column_type}"
                    )
                )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
