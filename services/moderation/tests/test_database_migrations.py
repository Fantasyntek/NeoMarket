import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app import database


def test_ticket_status_migration_replaces_moderated_with_approved(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_engine = create_engine(
        f"sqlite:///{tmp_path / 'moderation-migration.db'}"
    )
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE product_moderation ("
                "id VARCHAR(36) PRIMARY KEY, "
                "status VARCHAR(32) NOT NULL, "
                "CONSTRAINT ck_product_moderation_status CHECK "
                "(status IN ('PENDING', 'IN_REVIEW', 'MODERATED', "
                "'BLOCKED', 'HARD_BLOCKED', 'ARCHIVED'))"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO product_moderation (id, status) "
                "VALUES ('legacy-ticket', 'MODERATED')"
            )
        )
    monkeypatch.setattr(database, "engine", migration_engine)

    database._upgrade_ticket_status_schema()

    with migration_engine.begin() as connection:
        migrated_status = connection.execute(
            text(
                "SELECT status FROM product_moderation "
                "WHERE id = 'legacy-ticket'"
            )
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO product_moderation (id, status) "
                "VALUES ('approved-ticket', 'APPROVED')"
            )
        )
    assert migrated_status == "APPROVED"

    with pytest.raises(IntegrityError):
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO product_moderation (id, status) "
                    "VALUES ('invalid-ticket', 'MODERATED')"
                )
            )
    migration_engine.dispose()
