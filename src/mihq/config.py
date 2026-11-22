"""Configuration loading and validation.

WHY THIS FILE EXISTS
--------------------
Every secret (database password, Telegram bot token, Claude API key) lives in
a file called `.env` that is never committed to git. This module is the ONE
place that reads those values. Everything else in the app imports `Settings`
from here, so there is exactly one source of truth and zero hardcoded secrets.

HOW TO USE IT
-------------
    from mihq.config import load_settings

    settings = load_settings()          # raises ConfigError if something is missing
    print(settings.timezone)            # -> "America/New_York"

Secrets are stored in fields whose names end in `_secret`-ish terms, and the
`__repr__` below masks them so a stray `print(settings)` or a crash traceback
can never leak a token into your logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - dotenv is a declared dependency
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:  # type: ignore[misc]
        """Fallback so the app still runs if python-dotenv is absent.

        Real environment variables (for example those set by a hosting
        platform) still work; only reading a local .env file is skipped.
        """
        return False


# Project root = two directories above this file (src/mihq/config.py -> repo root)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

# Field names holding secrets. Used for masking in __repr__ and in logs.
SECRET_FIELDS = frozenset(
    {
        "database_url",
        "telegram_bot_token",
        "anthropic_api_key",
    }
)


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid.

    The message lists every problem at once so you can fix them in one pass
    instead of rerunning and discovering them one at a time.
    """


def mask(value: str | None, keep: int = 4) -> str:
    """Return a safe-to-log version of a secret.

    >>> mask("123456789:AAEabcdefg")
    '1234...(21 chars)'
    >>> mask("")
    '(empty)'
    >>> mask(None)
    '(not set)'
    """
    if value is None:
        return "(not set)"
    if value == "":
        return "(empty)"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}...({len(value)} chars)"


# --------------------------------------------------------------------------
# Small typed readers. Each one explains itself in the error message so a
# typo in .env produces a sentence you can act on, not a stack trace.
# --------------------------------------------------------------------------

def _get_str(name: str, default: str | None = None) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw != "" else default


def _get_int(name: str, default: int) -> int:
    raw = _get_str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a whole number, got {raw!r}. Fix it in your .env file."
        ) from exc


def _get_float(name: str, default: float) -> float:
    raw = _get_str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a number, got {raw!r}. Fix it in your .env file."
        ) from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = _get_str(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(
        f"{name} must be true or false, got {raw!r}. Fix it in your .env file."
    )


def _get_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Read a comma-separated list such as ENABLED_CHAINS=solana,base."""
    raw = _get_str(name)
    if raw is None:
        return default
    parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return parts if parts else default


@dataclass(frozen=True)
class Settings:
    """Every tunable value in the application, loaded once at startup.

    `frozen=True` makes this read-only after creation, so no module can
    quietly change configuration at runtime.
    """

    # --- Database -------------------------------------------------------
    database_url: str | None = None
    db_sslmode: str = "require"
    db_connect_timeout: int = 10

    # --- Telegram -------------------------------------------------------
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_admin_chat_id: str | None = None
    # How many pending alerts to send per pass. Keeps a backlog from
    # arriving as one overwhelming burst after an outage.
    notify_batch_size: int = 5
    # "compact" for a short phone-friendly alert, "full" for everything.
    alert_format: str = "compact"
    # Run contract security checks (honeypot, mint authority, LP lock,
    # holder concentration) on newly discovered tokens.
    security_checks_enabled: bool = True

    # --- Analysis -------------------------------------------------------
    # "template" costs nothing and needs no API key. "claude" adds
    # natural-language explanation and costs money per event analysed.
    analysis_mode: str = "template"

    # --- Claude ---------------------------------------------------------
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-5"
    claude_max_analyses_per_day: int = 40
    claude_max_output_tokens: int = 1200

    # --- Providers ------------------------------------------------------
    dexscreener_base_url: str = "https://api.dexscreener.com"
    dexscreener_rate_limit_per_min: int = 240
    geckoterminal_base_url: str = "https://api.geckoterminal.com/api/v2"
    geckoterminal_rate_limit_per_min: int = 25
    provider_cache_ttl_seconds: int = 20
    http_timeout_seconds: float = 15.0
    http_max_retries: int = 3

    # --- Scanners -------------------------------------------------------
    enabled_chains: tuple[str, ...] = field(
        default=("solana", "ethereum", "base", "bsc", "avalanche")
    )
    discovery_interval_seconds: int = 300
    watchlist_interval_seconds: int = 60
    watchlist_batch_size: int = 40

    # --- Snapshot write policy (see mihq.scanners.policy) ---------------
    snapshot_price_change_pct: float = 2.0
    snapshot_liquidity_change_pct: float = 5.0
    snapshot_volume_change_pct: float = 10.0
    snapshot_heartbeat_seconds: int = 900
    snapshot_raw_mode: str = "events_only"

    # --- Which categories are worth interrupting you for ----------------
    # Categories listed here are still detected, stored, and counted in the
    # daily summary - they simply never push to Telegram.
    #
    # `new_coin` is here by default because "a new pool exists" is a FEED,
    # not an alert. Measured on real data: 545 a day at a $1,000 floor, and
    # still 108 a day at $100,000. No threshold makes that a readable number
    # of push notifications, because the volume is real. Risk and momentum
    # events were about 10 a day over the same period, which is readable.
    notify_digest_only_categories: tuple[str, ...] = field(
        default_factory=lambda: ("new_coin",)
    )

    # --- Watchlist retirement (see mihq.scanners.retire) ----------------
    # Without these the watchlist only ever grows, and every entry costs API
    # calls and database rows on every pass, forever.
    # --- Health monitoring (see mihq.health) ----------------------------
    health_checks_enabled: bool = True
    # How long a scanner may go without a SUCCESSFUL pass before that counts
    # as a problem. Generous on purpose: GitHub's scheduled runs on a free
    # public repo arrive roughly hourly rather than on the cron written in
    # the workflow, so a tight threshold would cry wolf constantly.
    health_discovery_stale_hours: int = 6
    health_watchlist_stale_hours: int = 6
    # Supabase free tier is 500 MB.
    health_storage_limit_mb: int = 500
    health_storage_warn_pct: float = 70.0
    # A run still marked 'running' this long after it started was killed.
    health_abandoned_run_minutes: int = 60
    # Do not repeat the same KIND of system alert more often than this.
    health_alert_cooldown_hours: int = 12

    # What is worth watching in the first place. Until now the only gate was
    # "have we seen this token before", so EVERY discovered pool joined the
    # watchlist at any size - which is how it reached 2,367 entries in two
    # days. Retirement alone only churns against that inflow.
    watchlist_min_liquidity_usd: float = 10_000.0

    # How long a retired token stays off the list before discovery may add it
    # again. Without this, a token retired for being uneventful but still
    # well funded would be re-added on the very next discovery pass, and
    # bounce between states forever.
    watchlist_readd_cooldown_hours: int = 168   # one week

    watchlist_retire_enabled: bool = True
    watchlist_retire_below_liquidity_usd: float = 2_000.0
    watchlist_retire_grace_hours: int = 6
    watchlist_retire_uneventful_hours: int = 72

    # --- Event detection thresholds (see mihq.events.detectors) ---------
    event_min_liquidity_usd: float = 5_000.0
    # The new-token floor is SEPARATE from the general one, and deliberately
    # higher. Every newly discovered pool fires this detector exactly once,
    # so it is the single largest source of notifications: at $1,000 it
    # produced 66 of one day's 76 alerts. The other detectors fire only when
    # something changes, which is self-limiting; this one does not.
    event_new_token_min_liquidity_usd: float = 25_000.0
    event_min_volume_usd: float = 10_000.0
    event_volume_spike_pct: float = 150.0
    event_liquidity_drain_pct: float = 30.0
    event_price_surge_pct: float = 25.0
    event_price_collapse_pct: float = 30.0
    event_churn_ratio: float = 10.0

    # --- Daily summary --------------------------------------------------
    timezone: str = "America/New_York"
    daily_summary_hour: int = 8
    daily_summary_minute: int = 0

    # --- App ------------------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "text"
    app_env: str = "dev"
    dry_run: bool = False

    # -- Convenience ------------------------------------------------------

    @property
    def admin_chat_id(self) -> str | None:
        """Admin chat for health alerts, falling back to the main chat."""
        return self.telegram_admin_chat_id or self.telegram_chat_id

    def secrets_present(self) -> dict[str, bool]:
        """Which secrets are configured. Values are booleans, never the secret."""
        return {name: bool(getattr(self, name)) for name in sorted(SECRET_FIELDS)}

    def __repr__(self) -> str:  # pragma: no cover - formatting only
        parts = []
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in SECRET_FIELDS:
                parts.append(f"{f.name}={mask(value)!r}")
            else:
                parts.append(f"{f.name}={value!r}")
        return f"Settings({', '.join(parts)})"


# Which settings each development stage needs. `check_env.py` uses this to tell
# you what is missing for the stage you are working on, instead of demanding
# every credential on day one.
REQUIRED_BY_STAGE: dict[str, tuple[str, ...]] = {
    "database": ("database_url",),
    "telegram": ("telegram_bot_token", "telegram_chat_id"),
    "claude": ("anthropic_api_key",),
}


def load_settings(env_path: Path | str | None = None, *, override: bool = False) -> Settings:
    """Read .env plus real environment variables and return a `Settings`.

    Parameters
    ----------
    env_path:
        Path to the .env file. Defaults to `.env` in the project root.
        Missing file is fine: real environment variables are still read, which
        is how hosted deployments and GitHub Actions supply secrets.
    override:
        If True, values in .env win over already-set environment variables.
        Default False, which is the safer behaviour in production.

    Raises
    ------
    ConfigError
        If a value is present but unparseable (for example a non-numeric port).
        Missing-but-required values are reported by `validate_settings` instead,
        so you can still load settings for stages that do not need them.
    """
    path = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    load_dotenv(dotenv_path=path, override=override)

    return Settings(
        database_url=_get_str("DATABASE_URL"),
        db_sslmode=_get_str("DB_SSLMODE", "require") or "require",
        db_connect_timeout=_get_int("DB_CONNECT_TIMEOUT", 10),
        telegram_bot_token=_get_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get_str("TELEGRAM_CHAT_ID"),
        telegram_admin_chat_id=_get_str("TELEGRAM_ADMIN_CHAT_ID"),
        notify_batch_size=_get_int("NOTIFY_BATCH_SIZE", 5),
        alert_format=(_get_str("ALERT_FORMAT", "compact") or "compact").lower(),
        security_checks_enabled=_get_bool("SECURITY_CHECKS_ENABLED", True),
        analysis_mode=(_get_str("ANALYSIS_MODE", "template") or "template").lower(),
        anthropic_api_key=_get_str("ANTHROPIC_API_KEY"),
        anthropic_model=_get_str("ANTHROPIC_MODEL", "claude-sonnet-4-5") or "claude-sonnet-4-5",
        claude_max_analyses_per_day=_get_int("CLAUDE_MAX_ANALYSES_PER_DAY", 40),
        claude_max_output_tokens=_get_int("CLAUDE_MAX_OUTPUT_TOKENS", 1200),
        dexscreener_base_url=(
            _get_str("DEXSCREENER_BASE_URL", "https://api.dexscreener.com")
            or "https://api.dexscreener.com"
        ).rstrip("/"),
        dexscreener_rate_limit_per_min=_get_int("DEXSCREENER_RATE_LIMIT_PER_MIN", 240),
        geckoterminal_base_url=(
            _get_str("GECKOTERMINAL_BASE_URL", "https://api.geckoterminal.com/api/v2")
            or "https://api.geckoterminal.com/api/v2"
        ).rstrip("/"),
        geckoterminal_rate_limit_per_min=_get_int("GECKOTERMINAL_RATE_LIMIT_PER_MIN", 25),
        provider_cache_ttl_seconds=_get_int("PROVIDER_CACHE_TTL_SECONDS", 20),
        http_timeout_seconds=_get_float("HTTP_TIMEOUT_SECONDS", 15.0),
        http_max_retries=_get_int("HTTP_MAX_RETRIES", 3),
        enabled_chains=_get_csv(
            "ENABLED_CHAINS", ("solana", "ethereum", "base", "bsc", "avalanche")
        ),
        discovery_interval_seconds=_get_int("DISCOVERY_INTERVAL_SECONDS", 300),
        watchlist_interval_seconds=_get_int("WATCHLIST_INTERVAL_SECONDS", 60),
        watchlist_batch_size=_get_int("WATCHLIST_BATCH_SIZE", 40),
        snapshot_price_change_pct=_get_float("SNAPSHOT_PRICE_CHANGE_PCT", 2.0),
        snapshot_liquidity_change_pct=_get_float("SNAPSHOT_LIQUIDITY_CHANGE_PCT", 5.0),
        snapshot_volume_change_pct=_get_float("SNAPSHOT_VOLUME_CHANGE_PCT", 10.0),
        snapshot_heartbeat_seconds=_get_int("SNAPSHOT_HEARTBEAT_SECONDS", 900),
        snapshot_raw_mode=(
            _get_str("SNAPSHOT_RAW_MODE", "events_only") or "events_only"
        ).lower(),
        notify_digest_only_categories=_get_csv(
            "NOTIFY_DIGEST_ONLY_CATEGORIES", ("new_coin",)),
        health_checks_enabled=_get_bool("HEALTH_CHECKS_ENABLED", True),
        health_discovery_stale_hours=_get_int("HEALTH_DISCOVERY_STALE_HOURS", 6),
        health_watchlist_stale_hours=_get_int("HEALTH_WATCHLIST_STALE_HOURS", 6),
        health_storage_limit_mb=_get_int("HEALTH_STORAGE_LIMIT_MB", 500),
        health_storage_warn_pct=_get_float("HEALTH_STORAGE_WARN_PCT", 70.0),
        health_abandoned_run_minutes=_get_int("HEALTH_ABANDONED_RUN_MINUTES", 60),
        health_alert_cooldown_hours=_get_int("HEALTH_ALERT_COOLDOWN_HOURS", 12),
        watchlist_min_liquidity_usd=_get_float(
            "WATCHLIST_MIN_LIQUIDITY_USD", 10_000.0),
        watchlist_readd_cooldown_hours=_get_int(
            "WATCHLIST_READD_COOLDOWN_HOURS", 168),
        watchlist_retire_enabled=_get_bool("WATCHLIST_RETIRE_ENABLED", True),
        watchlist_retire_below_liquidity_usd=_get_float(
            "WATCHLIST_RETIRE_BELOW_LIQUIDITY_USD", 2_000.0),
        watchlist_retire_grace_hours=_get_int("WATCHLIST_RETIRE_GRACE_HOURS", 6),
        watchlist_retire_uneventful_hours=_get_int(
            "WATCHLIST_RETIRE_UNEVENTFUL_HOURS", 72),
        event_min_liquidity_usd=_get_float("EVENT_MIN_LIQUIDITY_USD", 5_000.0),
        event_new_token_min_liquidity_usd=_get_float(
            "EVENT_NEW_TOKEN_MIN_LIQUIDITY_USD", 25_000.0),
        event_min_volume_usd=_get_float("EVENT_MIN_VOLUME_USD", 10_000.0),
        event_volume_spike_pct=_get_float("EVENT_VOLUME_SPIKE_PCT", 150.0),
        event_liquidity_drain_pct=_get_float("EVENT_LIQUIDITY_DRAIN_PCT", 30.0),
        event_price_surge_pct=_get_float("EVENT_PRICE_SURGE_PCT", 25.0),
        event_price_collapse_pct=_get_float("EVENT_PRICE_COLLAPSE_PCT", 30.0),
        event_churn_ratio=_get_float("EVENT_CHURN_RATIO", 10.0),
        timezone=_get_str("TIMEZONE", "America/New_York") or "America/New_York",
        daily_summary_hour=_get_int("DAILY_SUMMARY_HOUR", 8),
        daily_summary_minute=_get_int("DAILY_SUMMARY_MINUTE", 0),
        log_level=(_get_str("LOG_LEVEL", "INFO") or "INFO").upper(),
        log_format=(_get_str("LOG_FORMAT", "text") or "text").lower(),
        app_env=(_get_str("APP_ENV", "dev") or "dev").lower(),
        dry_run=_get_bool("DRY_RUN", False),
    )


def validate_settings(settings: Settings, stages: tuple[str, ...] = ("database",)) -> list[str]:
    """Return a list of human-readable problems. Empty list means all good.

    `stages` names which groups of credentials must be present, using the keys
    of `REQUIRED_BY_STAGE`. This lets Stage A require only the database while
    later stages add Telegram and Claude.
    """
    problems: list[str] = []

    for stage in stages:
        if stage not in REQUIRED_BY_STAGE:
            problems.append(f"Unknown stage {stage!r} requested for validation.")
            continue
        for name in REQUIRED_BY_STAGE[stage]:
            if not getattr(settings, name):
                env_name = name.upper()
                problems.append(f"{env_name} is not set (needed for: {stage}).")

    if settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        problems.append(
            f"LOG_LEVEL is {settings.log_level!r}; expected DEBUG, INFO, WARNING, ERROR or CRITICAL."
        )

    if settings.log_format not in {"text", "json"}:
        problems.append(f"LOG_FORMAT is {settings.log_format!r}; expected 'text' or 'json'.")

    if not 0 <= settings.daily_summary_hour <= 23:
        problems.append(
            f"DAILY_SUMMARY_HOUR is {settings.daily_summary_hour}; expected 0-23."
        )

    if not 0 <= settings.daily_summary_minute <= 59:
        problems.append(
            f"DAILY_SUMMARY_MINUTE is {settings.daily_summary_minute}; expected 0-59."
        )

    if settings.discovery_interval_seconds < 30:
        problems.append(
            "DISCOVERY_INTERVAL_SECONDS below 30 risks hitting provider rate limits."
        )

    if settings.watchlist_interval_seconds < 20:
        problems.append(
            "WATCHLIST_INTERVAL_SECONDS below 20 risks hitting provider rate limits."
        )

    if settings.watchlist_batch_size < 1:
        problems.append("WATCHLIST_BATCH_SIZE must be at least 1.")

    if settings.claude_max_analyses_per_day < 0:
        problems.append("CLAUDE_MAX_ANALYSES_PER_DAY cannot be negative.")

    if not settings.enabled_chains:
        problems.append("ENABLED_CHAINS is empty; the scanner would have nothing to do.")

    if settings.alert_format not in {"compact", "full"}:
        problems.append(
            f"ALERT_FORMAT is {settings.alert_format!r}; expected compact or full."
        )

    if settings.analysis_mode not in {"template", "claude"}:
        problems.append(
            f"ANALYSIS_MODE is {settings.analysis_mode!r}; expected template or claude."
        )

    if settings.snapshot_raw_mode not in {"events_only", "always", "never"}:
        problems.append(
            f"SNAPSHOT_RAW_MODE is {settings.snapshot_raw_mode!r}; "
            "expected events_only, always or never."
        )

    if settings.snapshot_heartbeat_seconds < 60:
        problems.append(
            "SNAPSHOT_HEARTBEAT_SECONDS below 60 would write a snapshot almost "
            "every pass, which fills the database quickly."
        )

    if settings.event_new_token_min_liquidity_usd <= 0:
        problems.append(
            f"EVENT_NEW_TOKEN_MIN_LIQUIDITY_USD is "
            f"{settings.event_new_token_min_liquidity_usd}; expected a "
            f"positive number. Set it to the smallest pool you want to hear "
            f"about at all."
        )

    if settings.watchlist_retire_grace_hours < 1:
        problems.append(
            f"WATCHLIST_RETIRE_GRACE_HOURS is "
            f"{settings.watchlist_retire_grace_hours}; expected at least 1. "
            f"A brand-new pool is still filling, so retiring it immediately "
            f"would drop the tokens most worth watching."
        )

    if settings.event_min_liquidity_usd <= 0:
        problems.append(
            "EVENT_MIN_LIQUIDITY_USD must be above 0, or every dust token "
            "will generate alerts."
        )

    if settings.event_churn_ratio <= 1:
        problems.append(
            "EVENT_CHURN_RATIO at or below 1 would flag almost every active "
            "token as unusual."
        )

    for name, value in (
        ("SNAPSHOT_PRICE_CHANGE_PCT", settings.snapshot_price_change_pct),
        ("SNAPSHOT_LIQUIDITY_CHANGE_PCT", settings.snapshot_liquidity_change_pct),
        ("SNAPSHOT_VOLUME_CHANGE_PCT", settings.snapshot_volume_change_pct),
    ):
        if value < 0:
            problems.append(f"{name} cannot be negative.")

    # Validated here rather than in chains.py to keep that module import-free.
    from mihq.chains import unknown_chain_keys

    unknown = unknown_chain_keys(settings.enabled_chains)
    if unknown:
        problems.append(
            "ENABLED_CHAINS contains unknown chains: "
            + ", ".join(unknown)
            + ". Valid keys are listed in src/mihq/chains.py."
        )

    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001 - any tz failure is the same user-facing problem
        problems.append(
            f"TIMEZONE {settings.timezone!r} is not a valid IANA name "
            "(for example America/New_York). On Windows, make sure the "
            "'tzdata' package is installed."
        )

    return problems


def load_and_require(
    stages: tuple[str, ...] = ("database",),
    env_path: Path | str | None = None,
) -> Settings:
    """Load settings and raise `ConfigError` listing every problem at once."""
    settings = load_settings(env_path)
    problems = validate_settings(settings, stages)
    if problems:
        bullets = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(
            "Configuration is not usable yet:\n"
            f"{bullets}\n\n"
            "Fix these in your .env file (copy .env.example if you have not yet), "
            "then run: python scripts/check_env.py"
        )
    return settings
