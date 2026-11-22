"""The long-running process that drives both scanners.

HOW THE SCHEDULING WORKS
------------------------
One thread, two timers. Each loop knows when it is next due; the worker
sleeps until whichever comes first, runs it, and repeats.

Single-threaded is deliberate. Two threads sharing one database connection
is a classic source of bugs that only appear under load, and the work here is
almost entirely waiting on the network. If the two ever need to overlap, the
fix is two processes, not two threads.

A pass that overruns its interval does NOT queue up a backlog: the next due
time is computed from when the pass finished, so a slow minute cannot
snowball into a stampede of catch-up runs.

Run it:
    python -m mihq.worker              # run forever
    python -m mihq.worker --once       # one pass of each, then exit
    python -m mihq.worker --discovery  # just one discovery pass
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from mihq.config import ConfigError, Settings, load_and_require
from mihq.db.connection import connect
from mihq.db.repositories import Repository
from mihq.logging_setup import get_logger, setup_logging
from mihq.notify.dispatcher import NotificationDispatcher
from mihq.notify.telegram import TelegramClient
from mihq.providers.dexscreener import DexScreenerProvider
from mihq.providers.geckoterminal import GeckoTerminalProvider
from mihq.scanners.discovery import DiscoveryScanner
from mihq.scanners.policy import SnapshotPolicy
from mihq.scanners.watchlist import WatchlistScanner

log = get_logger(__name__)


@dataclass
class Loop:
    """One scheduled scanner."""

    name: str
    interval_seconds: int
    run: Callable[[], Any]
    next_due: float = 0.0


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._clock = clock
        self._sleep = sleep
        self._stopping = False

    def request_stop(self, *_args: Any) -> None:
        """Finish the current pass, then exit. Wired to SIGINT and SIGTERM."""
        if not self._stopping:
            log.info("stop requested; finishing the current pass")
        self._stopping = True

    def _build(
        self, connection: Any
    ) -> tuple[DiscoveryScanner, WatchlistScanner, NotificationDispatcher]:
        repo = Repository(connection)
        policy = SnapshotPolicy.from_settings(self.settings)
        discovery = DiscoveryScanner(
            self.settings, repo, GeckoTerminalProvider(self.settings), policy=policy
        )
        watchlist = WatchlistScanner(
            self.settings, repo, DexScreenerProvider(self.settings), policy=policy
        )
        notifier = NotificationDispatcher(self.settings, repo)
        return discovery, watchlist, notifier

    def run_once(self, *, which: str = "both", notify: bool = True) -> dict[str, Any]:
        """Run one pass of one or both scanners, then send anything pending."""
        results: dict[str, Any] = {}
        with connect(self.settings) as conn:
            discovery, watchlist, notifier = self._build(conn)
            if which in ("both", "discovery"):
                results["discovery"] = discovery.run_once()
            if which in ("both", "watchlist"):
                results["watchlist"] = watchlist.run_once()
            if which == "notify" or notify:
                results["notifications"] = notifier.dispatch_pending(
                    limit=self.settings.notify_batch_size
                )

            # Health checks run LAST and inside the same connection, so they
            # see the pass that just finished rather than the state before it.
            #
            # They are also the reason `send_system_alert` finally has a
            # caller - it was written in Stage F and left unconnected, which
            # made the code read as though failures were reported when
            # nothing ever reported one.
            if notify or which == "notify":
                results["health"] = self._check_health(
                    conn, notifier, results)
        return results

    def _check_health(self, conn: Any, notifier: Any,
                      results: dict[str, Any]) -> Any:
        """Never raises. A broken health check must not break the scan."""
        from mihq.health import HealthReport, check_run_outcome, run_health_checks

        try:
            repo = Repository(conn)
            report = run_health_checks(repo, notifier, self.settings)

            # Problems with the pass that just ran, which the database-level
            # checks cannot see: the run row says 'failed', but staleness
            # only notices once the gap grows.
            immediate = []
            for name in ("discovery", "watchlist"):
                outcome = results.get(name)
                if outcome is None:
                    continue
                immediate += check_run_outcome(
                    name,
                    getattr(outcome, "status", "success"),
                    getattr(outcome, "provider_errors", 0),
                )
            if immediate:
                from mihq.health import report_problems
                extra = report_problems(immediate, repo, notifier, self.settings)
                report.problems += extra.problems
                report.sent += extra.sent
                report.throttled += extra.throttled
                report.errors += extra.errors
            return report
        except Exception as exc:  # noqa: BLE001
            log.warning("health checks failed", extra={"error": str(exc)})
            from mihq.health import HealthReport as _HR
            return _HR(errors=[str(exc)])

    def run_daily_summary(
        self, *, for_date: Any = None, force: bool = False
    ) -> Any:
        """Build and send one day's summary. Defaults to yesterday, locally."""
        from mihq.summary import DailySummaryJob

        with connect(self.settings) as conn:
            job = DailySummaryJob(self.settings, Repository(conn))
            return job.run(for_date=for_date, force=force)

    def send_test_message(self) -> bool:
        """Prove the bot token and chat id work. Used by --test-telegram."""
        client = TelegramClient(self.settings)
        if not client.is_configured and not self.settings.dry_run:
            print("\nTELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set in .env.\n")
            return False
        result = client.send_message(
            "<b>MemeCoin Intelligence HQ</b>\n\n"
            "Connection test successful. Research notifications will arrive here.\n\n"
            "<i>Educational research only. Not investment advice.</i>"
        )
        if result.was_sent:
            print("\nSent. Check Telegram - the message should be there.\n")
        elif result.status == "skipped":
            print("\nDRY_RUN is on, so nothing was sent. The message above is "
                  "what would have gone out.\n")
        else:
            print(f"\nFailed: {result.error_message}\n")
        return result.ok

    def run_forever(self) -> None:
        """The production entrypoint."""
        if self.settings.dry_run:
            log.warning("DRY_RUN is on: no Telegram messages and no Claude calls")

        log.info(
            "worker starting",
            extra={
                "discovery_every": f"{self.settings.discovery_interval_seconds}s",
                "watchlist_every": f"{self.settings.watchlist_interval_seconds}s",
                "chains": ",".join(self.settings.enabled_chains),
            },
        )

        with connect(self.settings, autocommit=True) as conn:
            discovery, watchlist, notifier = self._build(conn)
            now = self._clock()

            def scan_and_notify(scanner):
                def run():
                    scanner.run_once()
                    notifier.dispatch_pending(limit=self.settings.notify_batch_size)
                return run

            loops = [
                Loop("discovery", self.settings.discovery_interval_seconds,
                     scan_and_notify(discovery), next_due=now),
                Loop("watchlist", self.settings.watchlist_interval_seconds,
                     scan_and_notify(watchlist), next_due=now),
            ]

            while not self._stopping:
                due = min(loops, key=lambda loop: loop.next_due)
                wait = due.next_due - self._clock()
                if wait > 0:
                    # Wake up at least once a second so a stop request is
                    # acted on promptly rather than after a full interval.
                    self._sleep(min(wait, 1.0))
                    continue

                try:
                    due.run()
                except Exception as exc:  # noqa: BLE001
                    # A scanner should handle its own errors; reaching here
                    # means a bug. Log it and keep the worker alive, because
                    # a crashed worker stops ALL monitoring.
                    log.error(
                        "unhandled error in a scanner pass",
                        extra={"scanner": due.name, "error": f"{type(exc).__name__}: {exc}"},
                    )

                # Measured from completion, so a slow pass cannot create a
                # backlog of catch-up runs.
                due.next_due = self._clock() + due.interval_seconds

        log.info("worker stopped")



def notify_fatal(settings: Any, exc: BaseException) -> str:
    """Tell the user over Telegram that the run died.

    Returns the send status - "sent", "skipped" (DRY_RUN or no token),
    "failed", or "error". Not a bool: "we deliberately did not send" and "we
    tried and could not" are different facts, and this is the one message
    whose job is to say truthfully whether the user was actually told.

    WHY THIS EXISTS SEPARATELY FROM THE HEALTH MONITOR

    Every check in `mihq.health` needs a database connection. When the
    database is the thing that is broken, none of them run - the process
    crashes inside `connect()` before any of them is reached, and the only
    trace is a failed GitHub workflow and whatever email GitHub chooses to
    send.

    Telegram does not need the database. So this path deliberately builds a
    bare client with no repository at all: it cannot log the attempt, and
    that is the point - it is the one report that still works when storage
    is gone.

    It never raises. A failure to report a crash must not replace the crash
    with a different one.
    """
    from mihq.db.connection import redact_dsn
    from mihq.notify.formatters import format_system_alert
    from mihq.notify.telegram import TelegramClient

    try:
        # The exception text can carry the connection string, password and
        # all, because psycopg puts the DSN in its errors.
        detail = redact_dsn(f"{type(exc).__name__}: {exc}")[:600]
        message = format_system_alert(
            "Scanner run crashed",
            f"The run stopped before it finished.\n\n{detail}",
            severity="critical",
        )
        return TelegramClient(settings).send_message(message).status
    except Exception:  # noqa: BLE001
        return "error"

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the MemeCoin Intelligence HQ scanners.")
    parser.add_argument("--once", action="store_true", help="Run one pass of each, then exit.")
    parser.add_argument("--discovery", action="store_true", help="Run one discovery pass only.")
    parser.add_argument("--watchlist", action="store_true", help="Run one watchlist pass only.")
    parser.add_argument("--notify", action="store_true",
                        help="Send pending notifications only, without scanning.")
    parser.add_argument("--test-telegram", action="store_true",
                        help="Send one test message to confirm the bot works.")
    parser.add_argument("--no-notify", action="store_true",
                        help="Scan but do not send anything.")
    parser.add_argument("--daily-summary", action="store_true",
                        help="Build and send one day's summary, then exit.")
    parser.add_argument("--for-date", metavar="YYYY-MM-DD",
                        help="Which local calendar day to summarise. "
                             "Defaults to yesterday in TIMEZONE.")
    parser.add_argument("--force", action="store_true",
                        help="Re-send a summary that was already sent.")
    args = parser.parse_args(argv)

    try:
        settings = load_and_require(stages=("database",))
    except ConfigError as exc:
        print(f"\nCannot start:\n{exc}\n", file=sys.stderr)
        return 1

    setup_logging(settings)
    worker = Worker(settings)

    if args.test_telegram:
        return 0 if worker.send_test_message() else 1

    if args.daily_summary:
        for_date = None
        if args.for_date:
            from datetime import date as _date
            try:
                for_date = _date.fromisoformat(args.for_date)
            except ValueError:
                print(f"\n--for-date must be YYYY-MM-DD, got {args.for_date!r}\n",
                      file=sys.stderr)
                return 1
        result = worker.run_daily_summary(for_date=for_date, force=args.force)
        print(result.describe())
        for error in result.errors:
            print(f"  ! {error}", file=sys.stderr)
        # A summary that was stored but not sent is not a failed run - a dry
        # run and an already-sent day both land here legitimately.
        return 0 if result.stored else 1

    if args.notify:
        results = worker.run_once(which="notify", notify=True)
        for result in results.values():
            print(result.summary())
        return 0

    if args.discovery or args.watchlist or args.once:
        which = "discovery" if args.discovery else "watchlist" if args.watchlist else "both"
        try:
            results = worker.run_once(which=which, notify=not args.no_notify)
        except Exception as exc:  # noqa: BLE001 - a crash the health checks
                                  # cannot report, because they need the
                                  # database that just failed
            log.error("run failed", extra={"error": f"{type(exc).__name__}: {exc}"})
            status = notify_fatal(settings, exc)
            explain = {
                "sent": "Telegram alert sent.",
                "skipped": "Telegram alert NOT sent (dry run, or no bot token).",
                "failed": "Telegram alert could NOT be delivered.",
                "error": "Telegram alert could not even be attempted.",
            }.get(status, f"Telegram alert status: {status}.")
            print(f"\nRun failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(explain, file=sys.stderr)
            return 1
        for result in results.values():
            print(result.summary())
        return 0

    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
