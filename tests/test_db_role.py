"""E-4 / T-1: the application's database role can use the data but not undo the audit
protection or change the schema."""
import contextlib

import asyncpg
import pytest

from rec.settings import settings
from rec.store import pg

TEST_PASSWORD = "rec-role-test"


@contextlib.asynccontextmanager
async def login_as(role: str):
    """A DSN that logs in as `role`, with a password set for the test and the old one put
    back afterwards: roles are cluster-wide, so a dev stack on the same Postgres logs in
    with them too. Needs the owner to be a superuser (it reads `pg_authid`)."""
    owner = await asyncpg.connect(settings.postgres_dsn)
    try:
        existed = await owner.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
        old = await owner.fetchval(
            "SELECT rolpassword FROM pg_authid WHERE rolname = $1", role)
        verb = "ALTER" if existed else "CREATE"
        await owner.execute(f"{verb} ROLE {role} LOGIN PASSWORD '{TEST_PASSWORD}'")
        await pg._migrate(await pg.pool())  # grants are applied by the migration
        base, db = settings.postgres_dsn.rsplit("/", 1)
        yield base.replace("//rec:rec@", f"//{role}:{TEST_PASSWORD}@") + "/" + db
    finally:
        if existed:  # the stored hash goes back as it was (a SCRAM verifier is accepted)
            secret = "NULL" if old is None else "'" + old.replace("'", "''") + "'"
            await owner.execute(f"ALTER ROLE {role} PASSWORD {secret}")
        await owner.close()


async def test_login_as_puts_the_old_password_back():
    """Running the suite against a dev stack's Postgres must not lock its services out."""
    owner = await asyncpg.connect(settings.postgres_dsn)
    try:
        await owner.execute(  # whatever password it has (or none) must survive
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rec_app') "
            "THEN CREATE ROLE rec_app LOGIN; END IF; END $$")
        before = await owner.fetchval(
            "SELECT rolpassword FROM pg_authid WHERE rolname = 'rec_app'")
        async with login_as("rec_app") as dsn:
            await (await asyncpg.connect(dsn)).close()
        assert await owner.fetchval(
            "SELECT rolpassword FROM pg_authid WHERE rolname = 'rec_app'") == before
    finally:
        await owner.close()


async def test_the_app_role_has_data_access_only():
    async with login_as("rec_app") as dsn:
        await _app_role_checks(dsn)


async def _app_role_checks(app_dsn: str) -> None:
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


async def test_the_analytics_role_reads_the_views_and_nothing_else():
    """Superset's role: every analytics view, no operational table, no raw envelope."""
    owner = await asyncpg.connect(settings.postgres_dsn)
    try:
        views = [r["table_name"] for r in await owner.fetch(
            "SELECT table_name FROM information_schema.views WHERE table_schema='analytics'")]
        exposed = {r["column_name"] for r in await owner.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='analytics'")}
    finally:
        await owner.close()
    assert len(views) >= 10
    assert not exposed & {"envelope", "reject_detail", "changes", "artifact_path"}
    async with login_as("rec_analytics") as dsn:
        await _analytics_role_checks(dsn, views)


async def _analytics_role_checks(dsn: str, views: list[str]) -> None:
    analyst = await asyncpg.connect(dsn)
    try:
        for view in views:
            await analyst.fetch(f"SELECT * FROM analytics.{view} LIMIT 1")  # nosec B608
        for statement in ("SELECT * FROM public.transaction_log LIMIT 1",
                          "SELECT * FROM public.customers LIMIT 1",
                          "SELECT * FROM public.audit_events LIMIT 1",
                          "CREATE TABLE analytics.probe (x INT)",
                          "DELETE FROM analytics.customers"):
            with pytest.raises((asyncpg.InsufficientPrivilegeError,
                                asyncpg.ObjectNotInPrerequisiteStateError)):
                await analyst.execute(statement)
    finally:
        await analyst.close()
