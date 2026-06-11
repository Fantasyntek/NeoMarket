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
    _upgrade_ticket_status_schema()
    _upgrade_product_moderation_schema()


def _upgrade_ticket_status_schema() -> None:
    inspector = inspect(engine)
    if "product_moderation" not in inspector.get_table_names():
        return
    status_constraints = [
        constraint
        for constraint in inspector.get_check_constraints("product_moderation")
        if constraint.get("name") == "ck_product_moderation_status"
    ]
    if status_constraints and "APPROVED" in str(
        status_constraints[0].get("sqltext", "")
    ):
        return

    if engine.dialect.name == "sqlite":
        _upgrade_sqlite_ticket_status()
        return
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE product_moderation "
                    "DROP CONSTRAINT IF EXISTS ck_product_moderation_status"
                )
            )
            connection.execute(
                text(
                    "UPDATE product_moderation SET status = 'APPROVED' "
                    "WHERE status = 'MODERATED'"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE product_moderation ADD CONSTRAINT "
                    "ck_product_moderation_status CHECK "
                    "(status IN ('PENDING', 'IN_REVIEW', 'APPROVED', "
                    "'BLOCKED', 'HARD_BLOCKED', 'ARCHIVED'))"
                )
            )


def _upgrade_sqlite_ticket_status() -> None:
    raw_connection = engine.raw_connection()
    cursor = raw_connection.cursor()
    try:
        create_sql_row = cursor.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'product_moderation'"
        ).fetchone()
        if create_sql_row is None or "MODERATED" not in create_sql_row[0]:
            return
        index_sql = [
            row[0]
            for row in cursor.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'product_moderation' "
                "AND sql IS NOT NULL"
            ).fetchall()
        ]
        columns = [
            row[1]
            for row in cursor.execute(
                "PRAGMA table_info(product_moderation)"
            ).fetchall()
        ]
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        selected_columns = ", ".join(
            (
                "CASE WHEN status = 'MODERATED' "
                "THEN 'APPROVED' ELSE status END"
            )
            if column == "status"
            else f'"{column}"'
            for column in columns
        )
        upgraded_create_sql = create_sql_row[0].replace(
            "'MODERATED'",
            "'APPROVED'",
        )

        cursor.execute("PRAGMA foreign_keys = OFF")
        cursor.execute("PRAGMA legacy_alter_table = ON")
        cursor.execute("BEGIN")
        cursor.execute(
            "ALTER TABLE product_moderation "
            "RENAME TO product_moderation_legacy_status"
        )
        cursor.execute(upgraded_create_sql)
        cursor.execute(
            f"INSERT INTO product_moderation ({quoted_columns}) "
            f"SELECT {selected_columns} "
            "FROM product_moderation_legacy_status"
        )
        cursor.execute("DROP TABLE product_moderation_legacy_status")
        for statement in index_sql:
            cursor.execute(statement)
        raw_connection.commit()
    except Exception:
        raw_connection.rollback()
        raise
    finally:
        cursor.execute("PRAGMA legacy_alter_table = OFF")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()
        raw_connection.close()


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
    if "content_revision" not in columns:
        statements.append(
            "ALTER TABLE product_moderation "
            "ADD COLUMN content_revision INTEGER NOT NULL DEFAULT 1"
        )
    if "review_revision" not in columns:
        statements.append(
            "ALTER TABLE product_moderation ADD COLUMN review_revision INTEGER"
        )
    if "decision_at" not in columns:
        statements.append(
            "ALTER TABLE product_moderation "
            f"ADD COLUMN decision_at {datetime_type}"
        )

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(
            text(
                "UPDATE product_moderation "
                "SET review_revision = content_revision "
                "WHERE status = 'IN_REVIEW' AND review_revision IS NULL"
            )
        )
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
