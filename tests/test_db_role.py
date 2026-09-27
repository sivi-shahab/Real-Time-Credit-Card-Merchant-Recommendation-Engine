"""E-4 / T-1: the application's database role can use the data but not undo the audit
protection or change the schema."""
import asyncpg
import pytest

from rec.settings import settings
from rec.store import pg

APP_PASSWORD = "rec-app-test"


async def test_the_app_role_has_data_access_only():
    owner = await asyncpg.connect(settings.postgres_dsn)
    try:
        if not await owner.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'rec_app'"):
            await owner.execute(f"CREATE ROLE rec_app LOGIN PASSWORD '{APP_PASSWORD}'")
        else:
            await owner.execute(f"ALTER ROLE rec_app PASSWORD '{APP_PASSWORD}'")
    finally:
        await owner.close()
    await pg._migrate(await pg.pool())  # grants are applied by the migration

    base, db = settings.postgres_dsn.rsplit("/", 1)
    app_dsn = base.replace("//rec:rec@", f"//rec_app:{APP_PASSWORD}@") + "/" + db
    app = await asyncpg.connect(app_dsn)
    try:
        # what the app does: read, write, and append to the audit trail
        await app.fetchval("SELECT count(*) FROM customers")
        await app.execute("UPDATE catalog_version SET version = version WHERE id = 1")
        audit_id = await app.fetchval(
            """INSERT INTO audit_events (actor, actor_role, action, resource, outcome)
               VALUES ('test', 'Test', 'test.e4', 'db', 'SUCCESS') RETURNING id""")
        # what it must not do
        for statement in (
            f"UPDATE audit_events SET outcome = 'X' WHERE id = {audit_id}",
            f"DELETE FROM audit_events WHERE id = {audit_id}",
            "TRUNCATE audit_events",
            "DROP TRIGGER audit_append_only ON audit_events",
            "ALTER TABLE audit_events DISABLE TRIGGER audit_append_only",
            "ALTER TABLE customers ADD COLUMN e4 INT",
            "CREATE TABLE e4_probe (x INT)",
            "DROP TABLE merchants",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await app.execute(statement)
    finally:
        await app.close()
