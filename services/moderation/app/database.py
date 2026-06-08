from collections.abc import Generator
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./neomarket-moderation.db")

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
    _upgrade_product_moderation_schema()


def _upgrade_product_moderation_schema() -> None:
    inspector = inspect(engine)
    if "product_moderation" not in inspector.get_table_names():
        return

    columns = {
        column["name"] for column in inspector.get_columns("product_moderation")
    }
    datetime_type = (
        "TIMESTAMP WITH TIME ZONE"
        if engine.dialect.name == "postgresql"
        else "DATETIME"
    )
    statements: list[str] = []
    if "kind" not in columns:
        statements.append(
            "ALTER TABLE product_moderation "
            "ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'CREATE'"
        )
    if "claimed_at" not in columns:
        statements.append(
            "ALTER TABLE product_moderation "
            f"ADD COLUMN claimed_at {datetime_type}"
        )
    if "claim_expires_at" not in columns:
        statements.append(
            "ALTER TABLE product_moderation "
            f"ADD COLUMN claim_expires_at {datetime_type}"
        )

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_product_moderation_claim_expires_at "
                "ON product_moderation (claim_expires_at)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_product_moderation_active_moderator "
                "ON product_moderation (moderator_id) "
                "WHERE status = 'IN_REVIEW'"
            )
        )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
