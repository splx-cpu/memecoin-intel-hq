"""How the app talks to Supabase PostgreSQL.

WHY A SEPARATE FILE
-------------------
Opening a database connection correctly involves several easy-to-forget
details: SSL must be on, there must be a timeout so a network blip cannot hang
the scanner forever, and connections must always be closed even when a query
raises. Doing that once here means no other module has to remember.

WHY psycopg (version 3) AND PLAIN SQL
-------------------------------------
An ORM such as SQLAlchemy adds a second thing to learn on top of SQL. Since
you are writing real migrations anyway, plain SQL with psycopg keeps one
mental model: what you type in the Supabase SQL editor is what you type here.

CONNECTION POOLING NOTE
-----------------------
Supabase gives you two kinds of connection string. Use the SESSION POOLER one
(port 5432) for this app. Supabase's free tier limits direct connections, and
the pooler is what survives a scanner restarting repeatedly.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator

from mihq.config import ConfigError, Settings
from mihq.logging_setup import get_logger

log = get_logger(__name__)

# psycopg is imported lazily so that modules which only need configuration
# (and the unit tests) work before you have installed the driver.
_PSYCOPG_INSTALL_HINT = (
    "The PostgreSQL driver is not installed. Run:\n"
    "    pip install -r requirements.txt\n"
    "(that installs psycopg[binary], which needs no compiler)"
)


def _import_psycopg() -> Any:
    try:
        import psycopg  # noqa: PLC0415 - deliberate lazy import
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ConfigError(_PSYCOPG_INSTALL_HINT) from exc
    return psycopg


# Matches the password in a postgres URL: scheme://user:PASSWORD@host
_DSN_PASSWORD_RE = re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)([^@\s]+)(@)")


def redact_dsn(text: str) -> str:
    """Replace the password inside any connection string in `text`.

    Driver errors often quote the whole DSN back at you. The host and user
    are useful for debugging and are kept; only the password is removed.

    >>> redact_dsn("could not connect to postgresql://user:hunter2@db.example.com:5432/x")
    'could not connect to postgresql://user:***@db.example.com:5432/x'
    >>> redact_dsn("password authentication failed")
    'password authentication failed'
    """
    return _DSN_PASSWORD_RE.sub(r"\1***\3", text)


class DatabaseError(RuntimeError):
    """Raised for connection problems, with a message aimed at a human."""


def _require_url(settings: Settings) -> str:
    if not settings.database_url:
        raise ConfigError(
            "DATABASE_URL is not set. Copy .env.example to .env and paste your "
            "Supabase connection string into it."
        )
    return settings.database_url


@contextmanager
def connect(settings: Settings, *, autocommit: bool = False) -> Iterator[Any]:
    """Open a connection, hand it over, and always close it afterwards.

    Usage:
        with connect(settings) as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")

    On a normal exit the transaction is committed; if the block raises, it is
    rolled back. That means a half-finished scanner pass can never leave the
    database in a partially-written state.

    `autocommit=True` is only needed for statements Postgres refuses to run
    inside a transaction (for example CREATE INDEX CONCURRENTLY).
    """
    psycopg = _import_psycopg()
    url = _require_url(settings)

    try:
        conn = psycopg.connect(
            url,
            autocommit=autocommit,
            connect_timeout=settings.db_connect_timeout,
            sslmode=settings.db_sslmode,
            application_name="mihq",
        )
    except Exception as exc:  # noqa: BLE001 - re-raised with guidance below
        # The driver's own message says WHICH thing went wrong - wrong
        # password, unknown host, timeout - and without it the caller is left
        # guessing. It can echo the connection string, so it is scrubbed
        # rather than withheld: a useless error is its own kind of bug.
        detail = redact_dsn(str(exc)).strip() or "(no detail provided by the driver)"
        raise DatabaseError(
            "Could not connect to the database.\n\n"
            f"  What the driver said: {detail}\n\n"
            "  Common causes:\n"
            "    - wrong password in DATABASE_URL (reset it in Supabase if unsure)\n"
            "    - the string is the Direct connection rather than the Session pooler\n"
            "    - [YOUR-PASSWORD] was left in place, brackets and all\n"
            "    - the Supabase project is paused\n"
            f"  Error type: {type(exc).__name__}"
        ) from exc

    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        if not autocommit:
            conn.commit()
    finally:
        conn.close()


def ping(settings: Settings) -> dict[str, Any]:
    """Prove the database is reachable. Returns server version and time.

    This is what `scripts/check_env.py` calls, and what the health monitor in
    Stage J uses to report database availability.
    """
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("select version(), now()")
            row = cur.fetchone()
    version = str(row[0]) if row else "unknown"
    return {
        "ok": True,
        "server_version": version.split(" on ")[0],
        "server_time": row[1] if row else None,
    }


def fetch_all(settings: Settings, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Run a read query and return every row.

    Always pass values via `params`, never by building the SQL string with
    f-strings. Parameters are what prevents SQL injection.
    """
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def fetch_one(settings: Settings, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    """Run a read query and return the first row, or None."""
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
