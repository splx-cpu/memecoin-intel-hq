"""Applies the SQL files in `migrations/` to the database, in order, once each.

WHY NOT JUST PASTE SQL INTO SUPABASE
------------------------------------
Pasting works the first time and becomes a problem on the fifth: you cannot
tell which changes a given database already has. A migration runner keeps a
table called `schema_migrations` listing every file it has applied, so running
it twice is harmless and running it on a fresh database rebuilds everything.

HOW IT WORKS
------------
1. Find every `*.sql` file in `migrations/`, sorted by filename. That is why
   they are numbered `001_`, `002_`, and so on - the numbers set the order.
2. Ask the database which ones are already recorded as applied.
3. Apply each missing file inside its own transaction. If one fails, that
   file's changes are rolled back entirely and the run stops, so the schema
   never ends up half-migrated.
4. Record the filename and a checksum of its contents.

The checksum catches a real beginner trap: editing a migration you already
applied. The file on disk would no longer match the database, and the runner
warns you instead of silently doing nothing. To change the schema, add a NEW
numbered file.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from mihq.config import PROJECT_ROOT, Settings
from mihq.db.connection import connect
from mihq.logging_setup import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

# Created before anything else, so the runner has somewhere to record itself.
_BOOTSTRAP_SQL = """
create table if not exists schema_migrations (
    filename    text        primary key,
    checksum    text        not null,
    applied_at  timestamptz not null default now()
);
"""


@dataclass(frozen=True)
class Migration:
    """One `.sql` file waiting to be applied."""

    filename: str
    path: Path
    sql: str
    checksum: str


@dataclass
class MigrationResult:
    """What a run did, for printing a summary and for tests."""

    applied: list[str]
    skipped: list[str]
    modified: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def checksum_of(sql: str) -> str:
    """Stable hash of a migration's contents.

    Line endings are normalized first so a file does not look "modified"
    merely because it was checked out on Windows (CRLF) instead of Linux (LF).
    """
    normalized = sql.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Read every `*.sql` file in the migrations folder, in filename order."""
    folder = directory or MIGRATIONS_DIR
    if not folder.is_dir():
        raise FileNotFoundError(
            f"Migrations folder not found: {folder}. "
            "Run this from the project root."
        )

    migrations: list[Migration] = []
    for path in sorted(folder.glob("*.sql"), key=lambda p: p.name):
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                filename=path.name,
                path=path,
                sql=sql,
                checksum=checksum_of(sql),
            )
        )

    if not migrations:
        raise FileNotFoundError(f"No .sql files found in {folder}.")
    return migrations


def applied_migrations(settings: Settings) -> dict[str, str]:
    """Filename -> checksum for everything already applied."""
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(_BOOTSTRAP_SQL)
            cur.execute("select filename, checksum from schema_migrations")
            return {str(row[0]): str(row[1]) for row in cur.fetchall()}


def run_migrations(
    settings: Settings,
    *,
    directory: Path | None = None,
    dry_run: bool = False,
) -> MigrationResult:
    """Apply any migrations the database does not have yet.

    `dry_run=True` reports what would happen and writes nothing - always worth
    running first against a database you care about.
    """
    migrations = discover_migrations(directory)
    already = applied_migrations(settings)

    result = MigrationResult(applied=[], skipped=[], modified=[])

    for migration in migrations:
        recorded = already.get(migration.filename)

        if recorded is not None:
            if recorded != migration.checksum:
                result.modified.append(migration.filename)
                log.warning(
                    "migration file changed after it was applied; "
                    "add a new numbered migration instead of editing this one",
                    extra={"migration": migration.filename},
                )
            else:
                result.skipped.append(migration.filename)
            continue

        if dry_run:
            result.applied.append(migration.filename)
            log.info("would apply migration", extra={"migration": migration.filename})
            continue

        log.info("applying migration", extra={"migration": migration.filename})
        # One connection per migration = one transaction per migration.
        with connect(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(migration.sql)
                cur.execute(
                    "insert into schema_migrations (filename, checksum) values (%s, %s)",
                    (migration.filename, migration.checksum),
                )
        result.applied.append(migration.filename)

    return result
