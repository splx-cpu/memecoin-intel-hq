"""Noticing that the system is broken, and saying so.

THE PROBLEM THIS SOLVES
-----------------------
Everything before this stage made the system work. Nothing made it tell you
when it had stopped working.

`send_system_alert` existed from Stage F and was never called from anywhere -
the capability was built and then left unconnected, which is worse than not
having it, because the code reads as though failures are reported.

THE SHAPE OF THE PROBLEM
------------------------
Failures divide into three kinds, and only the first is fully solvable here.

1. **The system runs and sees something wrong.** A pass failed, a provider is
   erroring, the database is filling up, a previous run was killed mid-pass.
   These it can detect and report, and that is what this module does.

2. **The system cannot run.** The database is unreachable, the bot token was
   revoked. It can often still report these, because the failure is usually
   in one subsystem and the reporting path is another. A database outage is
   reportable; a Telegram outage is not reportable VIA TELEGRAM.

3. **The system is not running at all.** GitHub suspended the schedule,
   the workflow file is broken, the repository was deleted. **Nothing in this
   file can detect these**, because nothing in this file executes. They are
   covered - partially - by the keepalive workflow and by GitHub's own
   failure emails. See docs/FAILURE_MODES.md, which is deliberately honest
   about what remains undetectable.

WHY THE COOLDOWN IS NOT A SIMPLE TIMER
--------------------------------------
Throttling is stored in the database. When the problem IS the database, the
throttle cannot be read - so those alerts fall back to being sent every run.
That turns out to be correct rather than a limitation: GitHub's schedule
delivers roughly one run an hour, so the worst case is an hourly reminder
that the database is down, which is a reasonable thing to be told hourly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from mihq.config import Settings
from mihq.logging_setup import get_logger

log = get_logger(__name__)

MB = 1024 * 1024


@dataclass(frozen=True)
class HealthProblem:
    """Something worth waking someone up about.

    `key` identifies the KIND of problem, not the instance. It is what the
    cooldown is keyed on, so it must not contain elapsed times, sizes or
    timestamps - otherwise every occurrence looks new and nothing is ever
    throttled.
    """
    key: str
    title: str
    detail: str
    severity: str = "high"


@dataclass
class HealthReport:
    problems: list[HealthProblem] = field(default_factory=list)
    sent: int = 0
    throttled: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.problems:
            return "health: no problems found"
        return (f"health: {len(self.problems)} problem(s), {self.sent} reported, "
                f"{self.throttled} throttled")


def _hours_since(moment: datetime | None, now: datetime) -> float | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds() / 3600


# ---------------------------------------------------------------------------
# The individual checks
# ---------------------------------------------------------------------------

def check_run_outcome(scanner: str, status: str, errors: int) -> list[HealthProblem]:
    """The pass that just finished.

    A `partial` is reported but is NOT escalated to critical: one chain's
    provider having a bad minute is normal and self-correcting. A `failed`
    pass means the whole thing achieved nothing.
    """
    if status == "failed":
        return [HealthProblem(
            key=f"run_failed:{scanner}",
            title=f"{scanner} scan failed",
            detail=(f"The most recent {scanner} pass did not complete. "
                    f"No data was collected from it."),
            severity="critical",
        )]
    if status == "partial" and errors:
        return [HealthProblem(
            key=f"run_partial:{scanner}",
            title=f"{scanner} scan partially failed",
            detail=(f"The {scanner} pass completed but {errors} provider "
                    f"call(s) failed. Coverage for this pass is incomplete."),
            severity="high",
        )]
    return []


def check_staleness(
    last_success: dict[str, datetime], settings: Settings, now: datetime
) -> list[HealthProblem]:
    """How long since each scanner last actually succeeded.

    This is the check that catches the quiet failures - a scanner erroring on
    every pass still "runs", and without this the only symptom is that alerts
    gradually stop arriving, which is indistinguishable from a calm market.
    """
    problems: list[HealthProblem] = []
    thresholds = {
        "discovery": settings.health_discovery_stale_hours,
        "watchlist": settings.health_watchlist_stale_hours,
    }

    for scanner, limit in thresholds.items():
        elapsed = _hours_since(last_success.get(scanner), now)

        if elapsed is None:
            # Never succeeded. On a brand-new install this is expected and
            # resolves itself within one pass, so it is reported at a lower
            # severity than a scanner that worked and then stopped.
            problems.append(HealthProblem(
                key=f"never_succeeded:{scanner}",
                title=f"{scanner} has never completed successfully",
                detail=(f"No successful {scanner} pass is on record. If this "
                        f"is a new deployment it should clear within an hour; "
                        f"if not, check the workflow logs."),
                severity="high",
            ))
        elif elapsed > limit:
            problems.append(HealthProblem(
                key=f"stale:{scanner}",
                title=f"{scanner} has not succeeded in {elapsed:.0f} hours",
                detail=(f"The last successful {scanner} pass was "
                        f"{elapsed:.1f} hours ago; the limit is {limit}. "
                        f"Either the scheduled runs have stopped or every "
                        f"recent pass has failed."),
                severity="critical",
            ))

    return problems


def check_abandoned_runs(
    abandoned: list[tuple[Any, ...]], settings: Settings
) -> list[HealthProblem]:
    """Runs still marked 'running' long after they started.

    The process was killed mid-pass - a GitHub runner timeout, an out-of-
    memory kill. Worth knowing because the pass neither succeeded nor
    reported a failure, so no other check would notice it.
    """
    if not abandoned:
        return []
    scanners = sorted({str(row[1]) for row in abandoned})
    return [HealthProblem(
        key="abandoned_runs",
        title=f"{len(abandoned)} scan(s) were killed mid-pass",
        detail=(f"Runs of {', '.join(scanners)} started but never finished, "
                f"and have been stuck for over "
                f"{settings.health_abandoned_run_minutes} minutes. The "
                f"process was terminated rather than failing normally."),
        severity="high",
    )]


def check_storage(size_bytes: int, settings: Settings) -> list[HealthProblem]:
    """How close the database is to the free-tier ceiling.

    Worth a warning well before it matters, because the failure mode when it
    fills is writes starting to error - which reads as a scanner bug rather
    than a quota, and would be debugged in entirely the wrong place.
    """
    limit_mb = settings.health_storage_limit_mb
    if limit_mb <= 0:
        return []

    used_mb = size_bytes / MB
    pct = used_mb / limit_mb * 100

    if pct >= 90:
        return [HealthProblem(
            key="storage_critical",
            title=f"Database is {pct:.0f}% full",
            detail=(f"{used_mb:.0f} MB of {limit_mb} MB used. Writes will "
                    f"start failing when this fills. Reduce retention, or "
                    f"set SNAPSHOT_RAW_MODE=never to stop storing provider "
                    f"payloads."),
            severity="critical",
        )]
    if pct >= settings.health_storage_warn_pct:
        return [HealthProblem(
            key="storage_warning",
            title=f"Database is {pct:.0f}% full",
            detail=(f"{used_mb:.0f} MB of {limit_mb} MB used. Not urgent "
                    f"yet, but worth planning for."),
            severity="high",
        )]
    return []


# ---------------------------------------------------------------------------
# Running them, and reporting
# ---------------------------------------------------------------------------

def collect_problems(
    repo: Any, settings: Settings, *, now: datetime | None = None
) -> tuple[list[HealthProblem], list[str]]:
    """Every check that needs the database. Returns (problems, errors).

    Each check is wrapped separately. A health monitor that falls over on its
    first failing query is worse than none, because it would most likely fail
    at exactly the moment something is wrong.
    """
    now = now or datetime.now(timezone.utc)
    problems: list[HealthProblem] = []
    errors: list[str] = []

    try:
        problems += check_staleness(repo.last_successful_runs(), settings, now)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"staleness check: {exc}")

    try:
        abandoned = repo.stale_running_runs(
            settings.health_abandoned_run_minutes * 60)
        problems += check_abandoned_runs(abandoned, settings)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"abandoned-run check: {exc}")

    try:
        problems += check_storage(repo.database_size_bytes(), settings)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"storage check: {exc}")

    return problems, errors


def report_problems(
    problems: list[HealthProblem],
    repo: Any,
    dispatcher: Any,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> HealthReport:
    """Send what is new, stay quiet about what has already been reported.

    Never raises. A failure to report a problem is logged and counted; it
    does not become a second problem.
    """
    now = now or datetime.now(timezone.utc)
    report = HealthReport(problems=list(problems))
    cooldown = settings.health_alert_cooldown_hours

    for problem in problems:
        if cooldown > 0:
            try:
                last = repo.last_system_alert(problem.key)
                elapsed = _hours_since(last, now)
                if elapsed is not None and elapsed < cooldown:
                    report.throttled += 1
                    continue
            except Exception as exc:  # noqa: BLE001
                # The throttle lives in the database. If the database is the
                # problem, we cannot read it - so we send. An extra alert
                # about a real failure beats a suppressed one.
                report.errors.append(f"cooldown lookup: {exc}")

        try:
            ok = dispatcher.send_system_alert(
                problem.title, problem.detail, severity=problem.severity,
                key=problem.key,
            )
            if ok:
                report.sent += 1
            else:
                report.errors.append(f"could not send: {problem.key}")
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"send failed for {problem.key}: {exc}")

    if problems:
        log.warning(report.summary(),
                    extra={"problems": [p.key for p in problems]})
    return report


def run_health_checks(
    repo: Any, dispatcher: Any, settings: Settings,
    *, now: datetime | None = None,
) -> HealthReport:
    """The whole thing, as called from the worker."""
    if not settings.health_checks_enabled:
        return HealthReport()

    problems, errors = collect_problems(repo, settings, now=now)
    report = report_problems(problems, repo, dispatcher, settings, now=now)
    report.errors = errors + report.errors
    return report
