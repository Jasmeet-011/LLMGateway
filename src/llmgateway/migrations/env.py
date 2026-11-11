"""Alembic's environment for the gateway's migrations (D-085, D-093).

Why it exists: Alembic runs this file to connect and apply `versions/`. The
migrations ship inside the package (script location `llmgateway:migrations`), so the
image can run them with no `alembic.ini`.

Key design choices:

- The database URL comes from `config.attributes["dsn"]`, set by
  `keystore.run_migrations`, never from a file or an ini option.
- Online, it connects through SQLAlchemy's asyncio engine on asyncpg, the driver the
  gateway uses, with no pool kept afterwards. Offline (`--sql`), it renders Postgres
  SQL with no connection, which the tests use to read the DDL.
- There is no model metadata: the migrations are written as SQL, so autogenerate is
  not used.
"""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def _apply(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def _online(dsn: str) -> None:
    url = make_url(dsn).set(drivername="postgresql+asyncpg")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_apply)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(dialect_name="postgresql", literal_binds=True, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_online(context.config.attributes["dsn"]))
