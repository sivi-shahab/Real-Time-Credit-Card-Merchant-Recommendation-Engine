import os
from pathlib import Path

# Own database: the E2E fixture TRUNCATEs tables, which must never hit the dev stack's data.
os.environ.setdefault("POSTGRES_DSN", "postgresql://rec:rec@localhost:55432/rec_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:56379/1")
os.environ.setdefault("DATA_DIR", str(Path(__file__).resolve().parents[1] / "data"))
os.environ.setdefault("ENVIRONMENT", "local")  # SIM-003: simulator needs local
os.environ.setdefault(
    "ADMIN_TOKENS",
    "admin-token:admin:Platform Operator,analyst-token:analyst:Analyst,"
    "ops-token:ops:Marketing Operator,auditor-token:auditor:Auditor,"
    "ml-token:mlops:ML Engineer,approver-token:approver:Approver,viewer-token:viewer:Viewer")
os.environ.setdefault("COOKIE_SECURE", "false")
# Many tests call the customer API for the same customer; the D-1 test lowers these itself.
os.environ.setdefault("RATE_LIMIT_RECOMMENDATIONS_PER_MINUTE", "1000000")
os.environ.setdefault("RATE_LIMIT_FEEDBACK_PER_MINUTE", "1000000")


def _ensure_database(dsn: str) -> None:
    import asyncio

    import asyncpg

    base, name = dsn.rsplit("/", 1)

    async def create():
        con = await asyncpg.connect(base + "/postgres")
        try:
            if not await con.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name):
                await con.execute(f'CREATE DATABASE "{name}"')
        finally:
            await con.close()

    asyncio.run(create())


_ensure_database(os.environ["POSTGRES_DSN"])
