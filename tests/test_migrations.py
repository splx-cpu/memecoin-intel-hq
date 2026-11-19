"""Tests for migration discovery and checksumming.

These run without a database. The schema itself is verified by applying the
migrations to a real PostgreSQL server - see docs/PROGRESS.md for the results
of that run.
"""

from __future__ import annotations

import re

import pytest

from mihq.db.migrate import MIGRATIONS_DIR, checksum_of, discover_migrations


def test_migrations_folder_exists():
    assert MIGRATIONS_DIR.is_dir(), f"missing folder: {MIGRATIONS_DIR}"


def test_all_migrations_discovered_in_numeric_order():
    migrations = discover_migrations()
    names = [m.filename for m in migrations]
    assert names == sorted(names)
    assert len(names) >= 11


def test_every_migration_is_numbered_and_named():
    """The number prefix is what fixes the apply order."""
    pattern = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")
    for migration in discover_migrations():
        assert pattern.match(migration.filename), f"bad filename: {migration.filename}"


def test_migration_numbers_are_unique():
    numbers = [m.filename[:3] for m in discover_migrations()]
    assert len(numbers) == len(set(numbers)), "two migrations share a number"


def test_no_migration_is_empty():
    for migration in discover_migrations():
        assert migration.sql.strip(), f"{migration.filename} is empty"


def test_all_nine_required_tables_are_created():
    """The spec lists these nine by name. None may be missing."""
    combined = "\n".join(m.sql for m in discover_migrations()).lower()
    required = [
        "tokens", "market_snapshots", "research_reports", "watchlists",
        "risk_flags", "alert_events", "notification_logs", "daily_summaries",
        "scanner_runs",
    ]
    for table in required:
        assert f"create table if not exists {table}" in combined, f"missing table: {table}"


def test_no_hardcoded_credentials_in_migrations():
    """A password pasted into a migration would be committed to git forever."""
    combined = "\n".join(m.sql for m in discover_migrations())
    for leak in ("sk-ant-", "postgresql://", "password '", "bot7", "AAH"):
        assert leak not in combined, f"possible credential in a migration: {leak}"


def _sql_code_only(sql: str) -> str:
    """Strip `--` comments AND single-quoted string literals.

    Needed because `comment on column ... is '...the provider timestamp.'`
    contains the word "timestamp" as prose, not as a column type.
    """
    without_comments = re.sub(r"--.*", "", sql)
    return re.sub(r"'[^']*'", "''", without_comments)


def test_timestamps_use_timezone_aware_type():
    """Plain `timestamp` silently drops the offset; we must never use it."""
    for migration in discover_migrations():
        bad = re.findall(r"\btimestamp\b(?!tz)", _sql_code_only(migration.sql))
        assert not bad, f"{migration.filename} uses bare `timestamp`, use `timestamptz`"


def test_money_columns_never_use_float():
    """Floats cannot represent decimal prices exactly."""
    for migration in discover_migrations():
        body = _sql_code_only(migration.sql).lower()
        for bad_type in ("double precision", " real ", " float"):
            assert bad_type not in body, f"{migration.filename} uses {bad_type!r} for a value"


def test_rls_enabled_for_every_required_table():
    combined = "\n".join(m.sql for m in discover_migrations()).lower()
    for table in ("tokens", "market_snapshots", "alert_events", "research_reports",
                  "notification_logs", "daily_summaries", "scanner_runs",
                  "watchlists", "risk_flags"):
        assert f"alter table {table}" in combined and "enable row level security" in combined


def test_checksum_is_stable_and_line_ending_agnostic():
    """A Windows checkout must not look like an edited migration."""
    unix = "create table x();\nselect 1;\n"
    windows = "create table x();\r\nselect 1;\r\n"
    assert checksum_of(unix) == checksum_of(windows)
    assert checksum_of(unix) != checksum_of("create table y();\n")


def test_checksum_changes_when_content_changes():
    first = discover_migrations()[0]
    assert checksum_of(first.sql + "\n-- edit") != first.checksum


def test_discover_raises_clearly_on_empty_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        discover_migrations(tmp_path)


def test_discover_raises_clearly_on_missing_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        discover_migrations(tmp_path / "nope")
