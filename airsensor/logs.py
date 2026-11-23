"""Structured logging: one JSON object per line.

Two loggers are used by the whole service:
- `events`       → logs/events.jsonl        (alerts, service life cycle, database, report)
- `connectivity` → logs/connectivity.jsonl  (source requests, internet probes, outages, browser)
Both also write to stdout. Files rotate at midnight UTC and the last 14 are kept.

Usage:
    events.info("alert_started", "Alert started in Стрийський район", raion="Стрийський район")
    events.error("unhandled_exception", "Task crashed", exc_info=True, task="collector")
"""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

EVENTS_LOGGER = "airsensor.events"
CONNECTIVITY_LOGGER = "airsensor.connectivity"
LOG_FILES = {EVENTS_LOGGER: "events.jsonl", CONNECTIVITY_LOGGER: "connectivity.jsonl"}
KEEP_DAYS = 14

# Two kinds of masking are applied to every log line:
# - registered secret values (the bot token, which aiogram may put into error messages and URLs);
# - the password part of any URL, e.g. postgresql://user:***@db/x. This is done by pattern, not by
#   value, because a password can be a common word (the default one is "airsensor").
_secrets: set[str] = set()
_URL_PASSWORD = re.compile(r"(://[^/:@\s\"]+:)[^@\s\"]+@")


def register_secret(value: str | None) -> None:
    """Mask this value wherever it appears in a log line. Use only for long, unique values such as tokens."""
    if value and len(value) >= 16:
        _secrets.add(value)


class JsonFormatter(logging.Formatter):
    """Formats a record as JSON: ts, level, logger, event, msg, the event's fields, and exc."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "level": record.levelname,
            "logger": record.name,
            # Records from libraries (uvicorn, asyncpg, aiogram) have no event code of their own.
            "event": getattr(record, "event", "library_log"),
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        line = json.dumps(entry, ensure_ascii=False, default=str)
        return _mask(line)


def _mask(line: str) -> str:
    for secret in _secrets:
        line = line.replace(secret, "***")
        line = line.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "***")  # JSON-escaped form
    return _URL_PASSWORD.sub(r"\1***@", line)


def describe(err: BaseException) -> str:
    """A short, single-line description of an exception for the `error` field of a log line."""
    lines = str(err).strip().splitlines()
    return f"{type(err).__name__}: {lines[0]}" if lines else type(err).__name__


class EventLogger:
    """A thin wrapper so every log call names an event code and passes its fields as keywords."""

    def __init__(self, name: str) -> None:
        self.logger = logging.getLogger(name)

    def log(self, level: int, event: str, msg: str, exc_info: bool = False, **fields: object) -> None:
        self.logger.log(level, msg, extra={"event": event, "fields": fields}, exc_info=exc_info)

    def debug(self, event: str, msg: str, **fields: object) -> None:
        self.log(logging.DEBUG, event, msg, **fields)

    def info(self, event: str, msg: str, **fields: object) -> None:
        self.log(logging.INFO, event, msg, **fields)

    def warning(self, event: str, msg: str, **fields: object) -> None:
        self.log(logging.WARNING, event, msg, **fields)

    def error(self, event: str, msg: str, exc_info: bool = False, **fields: object) -> None:
        self.log(logging.ERROR, event, msg, exc_info=exc_info, **fields)


events = EventLogger(EVENTS_LOGGER)
connectivity = EventLogger(CONNECTIVITY_LOGGER)


def setup_logging(log_dir: str, level: str = "INFO") -> None:
    """Attach the JSON handlers. If a log file can't be opened, keep logging to stdout and say so."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")  # Cyrillic on any console
    formatter = JsonFormatter()
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)

    # Libraries log through the root logger: only their warnings and errors are shown.
    root = logging.getLogger()
    root.handlers = [stdout]
    root.setLevel(logging.WARNING)

    file_problems = []
    for name, filename in LOG_FILES.items():
        logger = logging.getLogger(name)
        logger.handlers = [stdout]
        logger.setLevel(level)
        logger.propagate = False
        path = Path(log_dir) / filename
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = TimedRotatingFileHandler(
                path, when="midnight", backupCount=KEEP_DAYS, encoding="utf-8", utc=True
            )
        except OSError as err:
            file_problems.append((str(path), f"{type(err).__name__}: {err}"))
            continue
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    for path, error in file_problems:
        events.error("log_file_unavailable", f"Cannot write {path}; logging to stdout only", path=path, error=error)
