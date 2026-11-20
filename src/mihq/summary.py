"""The once-a-day research summary.

WHAT THIS IS FOR
----------------
Two jobs, and the second matters more than the first.

1. A digest of what the scanners saw yesterday.
2. **Proof the system is alive.** Alerts only arrive when something happens,
   so a silent phone is ambiguous: either the market was quiet or the scanner
   died three days ago and nobody noticed. A message that arrives every day
   regardless resolves that ambiguity, which is why the health section is
   printed even when every number in it is zero, and why a day with no
   successful scanner run says so in the first line of the narrative instead
   of reporting a tidy row of zeros.

WHY A LOCAL CALENDAR DAY, NOT "THE LAST 24 HOURS"
-------------------------------------------------
"Yesterday" is a human unit. It starts at local midnight and ends at the next
local midnight, and on the two days a year when the clocks change it is 23 or
25 hours long. `day_window` computes those two instants from the configured
timezone; everything downstream just receives two UTC timestamps.

Getting this wrong is not cosmetic: a fixed 24-hour subtraction would, twice
a year, either double-count an hour of alerts or drop one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from mihq.config import Settings
from mihq.db.repositories import Repository
from mihq.logging_setup import get_logger
from mihq.notify.formatters import esc, format_daily_summary
from mihq.notify.telegram import TelegramClient

log = get_logger(__name__)

# How many individual events the summary is willing to name.
MAX_HIGHLIGHTS = 5


# ---------------------------------------------------------------------------
# Day boundaries
# ---------------------------------------------------------------------------

def day_window(summary_date: date, tz_name: str) -> tuple[datetime, datetime]:
    """The UTC instants bounding one local calendar day: [start, end).

    DST is handled by construction rather than by arithmetic. Both ends are
    built as local midnight and then converted, so the gap between them is
    whatever that day actually lasted - 23 hours in spring, 25 in autumn, 24
    the rest of the year. Adding `timedelta(hours=24)` to the start would be
    wrong on exactly those two days.
    """
    tz = ZoneInfo(tz_name)
    start_local = datetime.combine(summary_date, time(0, 0), tzinfo=tz)
    end_local = datetime.combine(summary_date + timedelta(days=1), time(0, 0), tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def default_summary_date(now_utc: datetime, tz_name: str) -> date:
    """Yesterday, locally.

    The job summarises the COMPLETED day, not the one in progress. A summary
    sent at 08:00 about the current day would cover eight hours and read as
    though the other sixteen were quiet.
    """
    local_now = now_utc.astimezone(ZoneInfo(tz_name))
    return (local_now - timedelta(days=1)).date()


# ---------------------------------------------------------------------------
# Gathering the numbers
# ---------------------------------------------------------------------------

@dataclass
class DailySummaryResult:
    summary_date: str = ""
    stored: bool = False
    sent: bool = False
    skipped_reason: str | None = None
    summary_id: int | None = None
    message_chars: int = 0
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.skipped_reason:
            return f"daily summary {self.summary_date}: skipped ({self.skipped_reason})"
        state = "sent" if self.sent else "stored, not sent"
        return (f"daily summary {self.summary_date}: {state}"
                f" ({self.message_chars} chars)"
                + (f", {len(self.errors)} errors" if self.errors else ""))


def _short_address(address: str | None) -> str:
    if not address:
        return "unknown"
    return address if len(address) <= 12 else f"{address[:6]}...{address[-4:]}"


def _headline_for(row: tuple[Any, ...]) -> str:
    """One line naming an event, built only from stored columns.

    Deliberately descriptive, never evaluative: "liquidity drain" states the
    measurement, and nothing here is allowed to call it a scam, a rug, or an
    opportunity. The payload's own numbers are quoted when present and
    omitted when not, rather than defaulted to zero.
    """
    chain, symbol, address, event_type, severity, _triggered_at, payload = row

    label = (symbol or _short_address(address)).strip() or "unknown token"
    readable = str(event_type or "event").replace("_", " ")

    detail = ""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = None
    if isinstance(payload, dict):
        change = payload.get("change_pct")
        if isinstance(change, (int, float)):
            detail = f" ({change:+.0f}%)"

    return f"[{severity}] {label} on {chain}: {readable}{detail}"


def build_stats(
    repo: Repository, start: datetime, end: datetime
) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, int]]:
    """Everything the message needs, read once from the database.

    Returns (stats, highlights, watchlist_changes). Each query is wrapped
    separately: one failing section records an error and leaves its numbers
    absent rather than aborting the whole summary. A partial digest that says
    which part is missing is more useful than no digest at all.
    """
    stats: dict[str, Any] = {}
    errors: list[str] = []

    totals_by_scanner: dict[str, Any] = {}
    try:
        for row in repo.summary_run_totals(start, end):
            totals_by_scanner[row["scanner"]] = row
    except Exception as exc:  # noqa: BLE001
        errors.append(f"run totals: {exc}")

    def _total(key: str) -> int:
        return sum(int(r.get(key) or 0) for r in totals_by_scanner.values())

    stats["new_tokens"] = _total("new_tokens")
    stats["tokens_seen"] = _total("tokens_seen")
    stats["snapshots_written"] = _total("snapshots_written")
    stats["events_created"] = _total("events_created")
    stats["events_suppressed"] = _total("events_suppressed")

    stats["runs"] = {
        name: {
            "total": int(r.get("runs") or 0),
            "successful": int(r.get("successful") or 0),
            "partial": int(r.get("partial") or 0),
            "failed": int(r.get("failed") or 0),
        }
        for name, r in sorted(totals_by_scanner.items())
    }

    alerts: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    try:
        for category, severity, count in repo.summary_alerts(start, end):
            alerts[category] = alerts.get(category, 0) + count
            by_severity[severity] = by_severity.get(severity, 0) + count
    except Exception as exc:  # noqa: BLE001
        errors.append(f"alerts: {exc}")
    stats["alerts"] = alerts
    stats["alerts_by_severity"] = by_severity

    risk_flags: dict[str, int] = {}
    try:
        for flag_type, _severity, count in repo.summary_risk_flags_raised(start, end):
            risk_flags[flag_type] = risk_flags.get(flag_type, 0) + count
    except Exception as exc:  # noqa: BLE001
        errors.append(f"risk flags: {exc}")
    stats["risk_flags_raised"] = risk_flags

    watchlist_changes = {"added": 0, "removed": 0}
    try:
        added, removed = repo.summary_watchlist_delta(start, end)
        watchlist_changes = {"added": added, "removed": removed}
    except Exception as exc:  # noqa: BLE001
        errors.append(f"watchlist delta: {exc}")

    try:
        stats["watchlist_size"] = repo.active_watchlist_count()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"watchlist size: {exc}")

    highlights: list[dict[str, str]] = []
    try:
        for row in repo.summary_highlights(start, end, MAX_HIGHLIGHTS):
            highlights.append({"headline": _headline_for(row)})
    except Exception as exc:  # noqa: BLE001
        errors.append(f"highlights: {exc}")

    successful = sum(int(r.get("successful") or 0) for r in totals_by_scanner.values())
    failed = sum(int(r.get("failed") or 0) for r in totals_by_scanner.values())
    partial = sum(int(r.get("partial") or 0) for r in totals_by_scanner.values())
    finishes = [r.get("last_finished") for r in totals_by_scanner.values()
                if r.get("last_finished")]

    stats["health"] = {
        "successful_runs": successful,
        "failed_runs": failed,
        "partial_runs": partial,
        "provider_errors": _total("provider_errors"),
        "telegram_errors": _total("telegram_errors"),
        "database_errors": _total("database_errors"),
        "last_success": max(finishes).isoformat() if finishes else None,
    }

    stats["query_errors"] = errors
    return stats, highlights, watchlist_changes


# ---------------------------------------------------------------------------
# The narrative
# ---------------------------------------------------------------------------

def compose_narrative(stats: dict[str, Any]) -> str:
    """Two or three sentences describing the day, from measured values only.

    The rules the template analyzer follows apply here too: state what was
    counted, never why it happened and never what it implies. "Volume spiked
    on six tokens" is a measurement; "interest is building" is a story, and
    this function is not allowed to tell one.
    """
    health = stats.get("health") or {}
    successful = int(health.get("successful_runs") or 0)
    failed = int(health.get("failed_runs") or 0)

    # The alarming case leads, because it changes how every other number
    # below it should be read.
    if successful == 0:
        if failed:
            return (f"No scanner pass completed successfully. {failed} failed. "
                    f"The counts below cover only what was recorded before "
                    f"those failures and are not a picture of the market.")
        return ("No scanner pass ran at all during this period, so there is "
                "nothing measured to report. This is a system condition, not "
                "a quiet market.")

    new_tokens = int(stats.get("new_tokens") or 0)
    alerts = stats.get("alerts") or {}
    total_alerts = sum(alerts.values())
    severities = stats.get("alerts_by_severity") or {}
    serious = int(severities.get("critical", 0)) + int(severities.get("high", 0))

    def plural(count: int, one: str, many: str) -> str:
        return one if count == 1 else many

    parts = [
        f"{successful} scanner {plural(successful, 'pass', 'passes')} completed, "
        f"discovering {new_tokens} "
        f"{plural(new_tokens, 'token', 'tokens')} not seen before."
    ]

    if total_alerts == 0:
        parts.append("No alerts crossed their thresholds.")
    else:
        sentence = (f"{total_alerts} {plural(total_alerts, 'alert', 'alerts')} "
                    f"{plural(total_alerts, 'was', 'were')} raised")
        if serious:
            sentence += f", {serious} of them high or critical"
        parts.append(sentence + ".")

    flags = stats.get("risk_flags_raised") or {}
    if flags:
        total_flags = sum(flags.values())
        kinds = len(flags)
        parts.append(f"{total_flags} risk "
                     f"{plural(total_flags, 'flag was', 'flags were')} raised "
                     f"across {kinds} categor{'y' if kinds == 1 else 'ies'}.")

    if failed:
        parts.append(f"{failed} {plural(failed, 'pass', 'passes')} failed; "
                     f"coverage for this period is incomplete.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

class DailySummaryJob:
    """Builds, stores and sends one day's summary.

    Safe to run more than once for the same date. The second run refreshes
    the stored numbers and does NOT send a second message, because the
    database row remembers that it was already sent. That property is
    enforced by the upsert's deliberate omission of `status` from its update
    list (see SQL_UPSERT_DAILY_SUMMARY), not by a check in this class, so it
    holds even if two runs overlap.
    """

    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        client: TelegramClient | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repository
        self.client = client or TelegramClient(settings)

    def run(
        self,
        *,
        for_date: date | None = None,
        force: bool = False,
        now_utc: datetime | None = None,
    ) -> DailySummaryResult:
        """Never raises. Failures are reported in the result."""
        tz_name = self.settings.timezone
        now = now_utc or datetime.now(timezone.utc)
        summary_date = for_date or default_summary_date(now, tz_name)
        result = DailySummaryResult(summary_date=summary_date.isoformat())

        try:
            start, end = day_window(summary_date, tz_name)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"bad timezone {tz_name!r}: {exc}")
            return result

        stats, highlights, watchlist_changes = build_stats(self.repo, start, end)
        result.errors.extend(stats.get("query_errors") or [])

        stats["window_start_utc"] = start.isoformat()
        stats["window_end_utc"] = end.isoformat()
        stats["window_hours"] = round((end - start).total_seconds() / 3600, 2)

        narrative = compose_narrative(stats)

        try:
            summary_id, status_on_file, _was_new = self.repo.upsert_daily_summary(
                summary_date=summary_date,
                timezone_name=tz_name,
                stats=stats,
                narrative=narrative,
                watchlist_changes=watchlist_changes,
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"could not store summary: {exc}")
            log.error("daily summary not stored", extra={"error": str(exc)})
            return result

        result.stored = True
        result.summary_id = summary_id

        if status_on_file == "sent" and not force:
            result.skipped_reason = "already sent for this date"
            log.info(result.describe())
            return result

        message = format_daily_summary(
            stats=stats,
            narrative=narrative,
            summary_date=summary_date.isoformat(),
            timezone=tz_name,
            highlights=highlights,
        )
        result.message_chars = len(message)

        send = self.client.send_message(message)
        result.sent = send.status == "sent"

        try:
            self.repo.log_notification(
                alert_event_id=None,
                message_type="daily_summary",
                status=send.status,
                attempt=send.attempt,
                chat_id=send.chat_id,
                telegram_message_id=send.telegram_message_id,
                error_code=send.error_code,
                error_message=send.error_message,
            )
        except Exception as exc:  # noqa: BLE001 - logging the attempt is
                                  # useful but never worth losing the send
            result.errors.append(f"notification log: {exc}")

        # 'skipped' is what DRY_RUN and a missing token both report. Recording
        # it as 'sent' would make a dry run look like a delivery and, worse,
        # suppress the real send when the token is finally configured.
        if send.status in ("sent", "failed"):
            try:
                self.repo.mark_daily_summary_sent(
                    summary_id,
                    status="sent" if send.was_sent else "failed",
                    telegram_message_id=send.telegram_message_id,
                )
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"could not mark summary sent: {exc}")
        else:
            result.skipped_reason = f"telegram reported {send.status}"

        if not send.was_sent and send.error_message:
            result.errors.append(f"telegram: {send.error_message}")

        log.info(result.describe())
        return result
