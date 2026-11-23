"""Daily report: alert count and duration per raion plus connection failures, sent via aiogram.

Optional: it runs only when both BOT_TOKEN and ADMIN_CHAT_ID are set. It is sent at
REPORT_TIME (Kyiv time) and covers the previous calendar day in Kyiv time.

Error handling for Telegram:
- network errors, Telegram server errors and flood control (RetryAfter) are retried 3 times;
- authorisation, bad-request, forbidden and other API errors are logged without retrying.

Manual run (prints the report, and sends it if the bot is configured):
    python -m airsensor.report               # yesterday
    python -m airsensor.report 2026-11-17    # a given day
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, time, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError

from airsensor.config import KYIV, RAIONS, Config
from airsensor.db import DB_ERRORS, Database
from airsensor.logs import describe, events

SEND_RETRIES = 3
OUTAGE_LABELS = {
    "source": "недоступність джерела даних",
    "internet": "відсутність інтернету",
    "service": "сервіс не працював",
}


def format_duration(seconds: float) -> str:
    """Ukrainian short form: '2 год 05 хв', '7 хв', '40 с'."""
    seconds = round(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} год {minutes:02d} хв"
    if minutes:
        return f"{minutes} хв"
    return f"{secs} с"


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Start and end of a calendar day in Kyiv time (23 or 25 hours long on DST days)."""
    start = datetime.combine(day, time(), tzinfo=KYIV)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=KYIV)
    return start, end


def next_report_at(now: datetime, report_time: time) -> datetime:
    """The next moment (aware, Kyiv time) at which the daily report is due."""
    local_now = now.astimezone(KYIV)
    target = datetime.combine(local_now.date(), report_time, tzinfo=KYIV)
    if target <= local_now:
        target = datetime.combine(local_now.date() + timedelta(days=1), report_time, tzinfo=KYIV)
    return target


async def build_report(db: Database, day: date) -> str:
    start, end = day_bounds(day)
    alerts = {row["raion"]: row for row in await db.alert_stats(start, end)}
    outages = {row["kind"]: row for row in await db.outage_stats(start, end)}

    lines = [f"Звіт AirSensor за {day:%d.%m.%Y} — Львівська область", "", "Тривоги по районах:"]
    for raion in RAIONS:
        row = alerts.get(raion)
        if row is None:
            lines.append(f"• {raion}: тривог не було")
        else:
            lines.append(
                f"• {raion}: кількість {row['alert_count']}, загалом {format_duration(row['total_s'])}, "
                f"найдовша {format_duration(row['longest_s'])}"
            )
    lines += ["", "Збої зв'язку:"]
    if not outages:
        lines.append("• збоїв не було")
    for kind, label in OUTAGE_LABELS.items():
        row = outages.get(kind)
        if row is not None:
            lines.append(f"• {label}: кількість {row['outage_count']}, загалом {format_duration(row['total_s'])}")
    return "\n".join(lines)


async def send_report(bot: Bot, chat_id: int, text: str, day: date) -> bool:
    """Send the report text. Returns True on success. Every failed attempt is logged."""
    for attempt in range(1, SEND_RETRIES + 2):
        try:
            await bot.send_message(chat_id, text)
            events.info("report_sent", f"Daily report for {day} sent", date=day, attempt=attempt)
            return True
        except TelegramRetryAfter as err:  # flood control: Telegram says how long to wait
            delay, error = float(err.retry_after), describe(err)
        except (TelegramNetworkError, TelegramServerError) as err:
            delay, error = 2.0 ** (attempt - 1), describe(err)
        except TelegramAPIError as err:  # unauthorised, bad request, forbidden, not found …: retrying won't help
            events.error("report_failed", f"Telegram rejected the report for {day}: {describe(err)}",
                         date=day, attempt=attempt, error=describe(err), will_retry=False)
            return False

        if attempt > SEND_RETRIES:
            events.error("report_failed", f"Could not send the report for {day} after {attempt} attempts",
                         date=day, attempt=attempt, error=error, will_retry=False)
            return False
        events.warning("report_failed", f"Sending the report for {day} failed; retrying in {delay:g}s",
                       date=day, attempt=attempt, error=error, will_retry=True, next_retry_s=delay)
        await asyncio.sleep(delay)
    return False


async def send_daily_report(config: Config, db: Database, day: date) -> bool:
    """Build and send the report for one day. Returns True if it was sent."""
    try:
        text = await build_report(db, day)
    except DB_ERRORS as err:
        events.error("report_failed", f"Could not build the report for {day}: database error",
                     date=day, attempt=1, error=describe(err), will_retry=False)
        return False
    assert config.bot_token is not None and config.admin_chat_id is not None
    bot = Bot(config.bot_token)
    try:
        return await send_report(bot, config.admin_chat_id, text, day)
    finally:
        await bot.session.close()


async def report_loop(config: Config, db: Database) -> None:
    """Background task: at REPORT_TIME every day, send the report for the previous day."""
    while True:
        due = next_report_at(datetime.now(timezone.utc), config.report_time)
        events.debug("report_scheduled", f"Next daily report at {due.isoformat()}", due=due)
        # Sleep in short steps against the wall clock, so a suspended host does not delay the report for hours.
        while (remaining := (due - datetime.now(timezone.utc)).total_seconds()) > 0:
            await asyncio.sleep(min(remaining, 60))
        await send_daily_report(config, db, due.date() - timedelta(days=1))


async def _cli(config: Config, day: date) -> int:
    db = Database(config.database_url)
    await db.connect(config.backoff_max_s)
    try:
        print(await build_report(db, day))
        if not config.report_enabled:
            print("\n(BOT_TOKEN / ADMIN_CHAT_ID not set: report printed only)")
            return 0
        return 0 if await send_daily_report(config, db, day) else 1
    finally:
        await db.close()


def main() -> None:
    from airsensor.main import load_config_or_exit  # imported here: main.py imports this module

    config = load_config_or_exit()
    try:
        day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(KYIV).date() - timedelta(days=1)
    except ValueError:
        print("usage: python -m airsensor.report [YYYY-MM-DD]", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_cli(config, day)))


if __name__ == "__main__":
    main()
