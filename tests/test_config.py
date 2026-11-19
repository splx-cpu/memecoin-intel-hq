"""Tests for configuration loading, validation and secret masking."""

from __future__ import annotations

import pytest

from mihq.config import (
    ConfigError,
    Settings,
    load_settings,
    mask,
    validate_settings,
)

# Every env var the loader reads, so each test starts from a clean slate.
ENV_KEYS = [
    "DATABASE_URL", "DB_SSLMODE", "DB_CONNECT_TIMEOUT",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_CHAT_ID",
    "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "CLAUDE_MAX_ANALYSES_PER_DAY",
    "CLAUDE_MAX_OUTPUT_TOKENS", "DEXSCREENER_BASE_URL",
    "DEXSCREENER_RATE_LIMIT_PER_MIN", "GECKOTERMINAL_BASE_URL",
    "GECKOTERMINAL_RATE_LIMIT_PER_MIN", "PROVIDER_CACHE_TTL_SECONDS",
    "HTTP_TIMEOUT_SECONDS", "HTTP_MAX_RETRIES", "ENABLED_CHAINS",
    "DISCOVERY_INTERVAL_SECONDS", "WATCHLIST_INTERVAL_SECONDS",
    "WATCHLIST_BATCH_SIZE", "TIMEZONE", "DAILY_SUMMARY_HOUR",
    "DAILY_SUMMARY_MINUTE", "LOG_LEVEL", "LOG_FORMAT", "APP_ENV", "DRY_RUN",
]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Remove all app env vars and point the loader at an empty .env."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    empty = tmp_path / ".env"
    empty.write_text("")
    return empty


# --- Masking: the thing that keeps secrets out of logs ---------------------

def test_mask_hides_the_body_of_a_secret():
    token = "123456789:AAEabcdefghijklmnopqrstuvwxyz0123456"
    masked = mask(token)
    assert masked.startswith("1234")
    assert "AAEabcdefg" not in masked
    assert token not in masked


def test_mask_handles_missing_and_short_values():
    assert mask(None) == "(not set)"
    assert mask("") == "(empty)"
    assert mask("abc") == "***"


def test_repr_never_leaks_a_secret():
    """A stray print(settings) or a traceback must not expose credentials."""
    secret_url = "postgresql://postgres:SUPERSECRETPW@db.example.com:5432/postgres"
    settings = Settings(
        database_url=secret_url,
        telegram_bot_token="123456789:AAEsecrettokenvalue1234567890abcdefg",
        anthropic_api_key="sk-ant-api03-REALKEYMATERIAL",
    )
    text = repr(settings)
    assert "SUPERSECRETPW" not in text
    assert "AAEsecrettokenvalue" not in text
    assert "REALKEYMATERIAL" not in text


def test_secrets_present_reports_booleans_not_values():
    settings = Settings(database_url="postgresql://u:p@h/db")
    presence = settings.secrets_present()
    assert presence["database_url"] is True
    assert presence["anthropic_api_key"] is False
    assert all(isinstance(v, bool) for v in presence.values())


# --- Loading ---------------------------------------------------------------

def test_defaults_apply_when_nothing_is_set(clean_env):
    settings = load_settings(clean_env)
    assert settings.timezone == "America/New_York"
    assert settings.discovery_interval_seconds == 300
    assert settings.watchlist_interval_seconds == 60
    assert settings.log_level == "INFO"
    assert settings.dry_run is False
    assert settings.database_url is None


def test_values_are_read_from_a_dotenv_file(tmp_path, monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "DATABASE_URL=postgresql://u:p@h:5432/postgres\n"
        "TIMEZONE=Europe/London\n"
        "ENABLED_CHAINS=base, solana\n"
        "DRY_RUN=true\n"
        "DISCOVERY_INTERVAL_SECONDS=120\n"
    )
    settings = load_settings(env)
    assert settings.database_url == "postgresql://u:p@h:5432/postgres"
    assert settings.timezone == "Europe/London"
    assert settings.enabled_chains == ("base", "solana")
    assert settings.dry_run is True
    assert settings.discovery_interval_seconds == 120


def test_whitespace_only_value_falls_back_to_default(clean_env, monkeypatch):
    monkeypatch.setenv("TIMEZONE", "   ")
    assert load_settings(clean_env).timezone == "America/New_York"


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("off", False),
])
def test_boolean_spellings(clean_env, monkeypatch, raw, expected):
    monkeypatch.setenv("DRY_RUN", raw)
    assert load_settings(clean_env).dry_run is expected


def test_bad_boolean_gives_an_actionable_error(clean_env, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "maybe")
    with pytest.raises(ConfigError, match="DRY_RUN"):
        load_settings(clean_env)


def test_bad_integer_gives_an_actionable_error(clean_env, monkeypatch):
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "five minutes")
    with pytest.raises(ConfigError, match="DISCOVERY_INTERVAL_SECONDS"):
        load_settings(clean_env)


def test_trailing_slash_stripped_from_provider_urls(clean_env, monkeypatch):
    monkeypatch.setenv("DEXSCREENER_BASE_URL", "https://api.dexscreener.com/")
    assert load_settings(clean_env).dexscreener_base_url == "https://api.dexscreener.com"


def test_admin_chat_falls_back_to_main_chat():
    assert Settings(telegram_chat_id="123").admin_chat_id == "123"
    assert Settings(telegram_chat_id="123", telegram_admin_chat_id="999").admin_chat_id == "999"


# --- Validation ------------------------------------------------------------

def test_missing_database_url_is_reported(clean_env):
    problems = validate_settings(load_settings(clean_env), ("database",))
    assert any("DATABASE_URL" in p for p in problems)


def test_valid_database_config_has_no_problems(clean_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/postgres")
    assert validate_settings(load_settings(clean_env), ("database",)) == []


def test_telegram_stage_requires_both_token_and_chat(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAEtoken")
    problems = validate_settings(load_settings(clean_env), ("telegram",))
    assert any("TELEGRAM_CHAT_ID" in p for p in problems)
    assert not any("TELEGRAM_BOT_TOKEN" in p for p in problems)


def test_unknown_chain_in_env_is_caught(clean_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("ENABLED_CHAINS", "base,dogechain")
    problems = validate_settings(load_settings(clean_env), ("database",))
    assert any("dogechain" in p for p in problems)


def test_invalid_timezone_is_caught(clean_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("TIMEZONE", "Mars/Olympus_Mons")
    problems = validate_settings(load_settings(clean_env), ("database",))
    assert any("TIMEZONE" in p for p in problems)


def test_out_of_range_summary_time_is_caught(clean_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("DAILY_SUMMARY_HOUR", "25")
    monkeypatch.setenv("DAILY_SUMMARY_MINUTE", "99")
    problems = validate_settings(load_settings(clean_env), ("database",))
    assert any("DAILY_SUMMARY_HOUR" in p for p in problems)
    assert any("DAILY_SUMMARY_MINUTE" in p for p in problems)


def test_too_aggressive_scan_intervals_are_flagged(clean_env, monkeypatch):
    """Protects the provider relationship: too-fast polling gets you blocked."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("DISCOVERY_INTERVAL_SECONDS", "5")
    monkeypatch.setenv("WATCHLIST_INTERVAL_SECONDS", "2")
    problems = validate_settings(load_settings(clean_env), ("database",))
    assert any("DISCOVERY_INTERVAL_SECONDS" in p for p in problems)
    assert any("WATCHLIST_INTERVAL_SECONDS" in p for p in problems)


def test_empty_chain_list_is_flagged(clean_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    settings = load_settings(clean_env)
    settings = Settings(**{**settings.__dict__, "enabled_chains": ()})
    problems = validate_settings(settings, ("database",))
    assert any("ENABLED_CHAINS" in p for p in problems)
