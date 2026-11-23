"""Configuration: fixed facts about the data source, and the environment variables.

Every environment variable is read and validated once, at startup, by `load_config()`.
All problems are collected and raised together as one `ConfigError`, so the operator
sees every bad setting in a single run. Problem messages never contain secret values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

# --- Fixed facts about the data source (verified on alerts.in.ua, 2026-11-18) ---

KYIV = ZoneInfo("Europe/Kyiv")
OBLAST = "Львівська область"
RAIONS = (
    "Львівський район",
    "Дрогобицький район",
    "Золочівський район",
    "Самбірський район",
    "Стрийський район",
    "Шептицький район",
    "Яворівський район",
)
# Alert types we expect. Anything else is still stored, with a warning.
KNOWN_ALERT_TYPES = frozenset({"air-raid", "artillery-shelling", "urban-fights", "chemical", "nuclear"})

# --- Facts about the JSON API behind the map (checked 2026-11-19) ---
# Location ids (`luid`) are the official alerts.in.ua location uids, e.g. 27 = Lviv oblast.
OBLAST_UID = 27
RAION_UIDS = {
    90: "Львівський район",
    91: "Дрогобицький район",
    94: "Золочівський район",
    88: "Самбірський район",
    89: "Стрийський район",
    92: "Шептицький район",
    93: "Яворівський район",
}

SOURCE_MODES = ("api", "browser")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(Exception):
    """One or more settings are invalid. `problems` is a list of (field, reason)."""

    def __init__(self, problems: list[tuple[str, str]]) -> None:
        self.problems = problems
        super().__init__("; ".join(f"{field}: {reason}" for field, reason in problems))


@dataclass(frozen=True)
class Config:
    """Validated settings. See .env.example for a description of each one."""

    source_mode: str
    api_url: str
    locations_url: str
    request_timeout_s: float
    source_url: str
    feed_url_prefix: str
    database_url: str
    poll_interval_s: float
    stale_after_s: float
    page_timeout_s: float
    backoff_max_s: float
    browser_restart_after: int
    page_reload_every_min: float
    probe_targets: tuple[tuple[str, int], ...]
    probe_interval_s: float
    probe_timeout_s: float
    report_time: time
    bot_token: str | None
    admin_chat_id: int | None
    log_level: str
    log_dir: str
    http_port: int

    @property
    def report_enabled(self) -> bool:
        return self.bot_token is not None and self.admin_chat_id is not None

    def masked(self) -> dict[str, object]:
        """The settings as a dict that is safe to log: secrets are replaced by '***'."""
        return {
            "source_mode": self.source_mode,
            "api_url": self.api_url,
            "locations_url": self.locations_url,
            "request_timeout_s": self.request_timeout_s,
            "source_url": self.source_url,
            "feed_url_prefix": self.feed_url_prefix,
            "database_url": mask_url_password(self.database_url),
            "poll_interval_s": self.poll_interval_s,
            "stale_after_s": self.stale_after_s,
            "page_timeout_s": self.page_timeout_s,
            "backoff_max_s": self.backoff_max_s,
            "browser_restart_after": self.browser_restart_after,
            "page_reload_every_min": self.page_reload_every_min,
            "probe_targets": [f"{host}:{port}" for host, port in self.probe_targets],
            "probe_interval_s": self.probe_interval_s,
            "probe_timeout_s": self.probe_timeout_s,
            "report_time": self.report_time.strftime("%H:%M"),
            "bot_token": "***" if self.bot_token else None,
            "admin_chat_id": self.admin_chat_id,
            "log_level": self.log_level,
            "log_dir": self.log_dir,
            "http_port": self.http_port,
        }


def mask_url_password(url: str) -> str:
    """Replace the password in a URL like postgresql://user:pass@host/db with '***'."""
    parts = urlsplit(url)
    if not parts.password:
        return url
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
    return urlunsplit(parts._replace(netloc=netloc))


def load_config(env: Mapping[str, str]) -> Config:
    """Build a Config from environment variables, or raise ConfigError listing every problem."""
    read = _Reader(env)
    bot_token = read.bot_token("BOT_TOKEN")
    admin_chat_id = read.integer("ADMIN_CHAT_ID", None, minimum=None)
    if bot_token and admin_chat_id is None and "ADMIN_CHAT_ID" not in read.failed:
        read.fail("ADMIN_CHAT_ID", "is required when BOT_TOKEN is set")
    if admin_chat_id is not None and not bot_token and "BOT_TOKEN" not in read.failed:
        read.fail("BOT_TOKEN", "is required when ADMIN_CHAT_ID is set")

    config = Config(
        source_mode=read.choice("SOURCE_MODE", "api", SOURCE_MODES, upper=False),
        api_url=read.url(
            "API_URL", "https://api.alerts.in.ua/v3/alerts/active.json", schemes=("http", "https")
        ),
        locations_url=read.url(
            "LOCATIONS_URL", "https://alerts.in.ua/assets/locations.json?v=5", schemes=("http", "https")
        ),
        request_timeout_s=read.number("REQUEST_TIMEOUT_S", 10, minimum=1),
        source_url=read.url("SOURCE_URL", "https://alerts.in.ua/", schemes=("http", "https")),
        feed_url_prefix=read.url(
            "FEED_URL_PREFIX", "https://api.alerts.in.ua/v3/alerts/active", schemes=("http", "https")
        ),
        database_url=read.url(
            "DATABASE_URL",
            "postgresql://airsensor:airsensor@localhost:5432/airsensor",
            schemes=("postgres", "postgresql"),
            secret=True,
        ),
        poll_interval_s=read.number("POLL_INTERVAL_S", 30, minimum=1),
        stale_after_s=read.number("STALE_AFTER_S", 120, minimum=10),
        page_timeout_s=read.number("PAGE_TIMEOUT_S", 30, minimum=1),
        backoff_max_s=read.number("BACKOFF_MAX_S", 60, minimum=1),
        browser_restart_after=read.integer("BROWSER_RESTART_AFTER", 5, minimum=1),
        page_reload_every_min=read.number("PAGE_RELOAD_EVERY_MIN", 30, minimum=1),
        probe_targets=read.probe_targets("PROBE_TARGETS", "8.8.8.8:53,1.1.1.1:53"),
        probe_interval_s=read.number("PROBE_INTERVAL_S", 30, minimum=1),
        probe_timeout_s=read.number("PROBE_TIMEOUT_S", 3, minimum=0.1),
        report_time=read.clock_time("REPORT_TIME", "08:00"),
        bot_token=bot_token,
        admin_chat_id=admin_chat_id,
        log_level=read.choice("LOG_LEVEL", "INFO", LOG_LEVELS),
        log_dir=read.text("LOG_DIR", "logs"),
        http_port=read.integer("HTTP_PORT", 8000, minimum=1, maximum=65535),
    )
    if read.problems:
        raise ConfigError(read.problems)
    return config


class _Reader:
    """Reads one variable at a time. On a bad value it records the problem and returns the default."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env
        self.problems: list[tuple[str, str]] = []
        self.failed: set[str] = set()

    def fail(self, name: str, reason: str) -> None:
        self.problems.append((name, reason))
        self.failed.add(name)

    def raw(self, name: str) -> str | None:
        """The trimmed value, or None when the variable is unset or empty."""
        value = self.env.get(name, "").strip()
        return value or None

    def text(self, name: str, default: str) -> str:
        return self.raw(name) or default

    def number(self, name: str, default: float, minimum: float) -> float:
        raw = self.raw(name)
        if raw is None:
            return float(default)
        try:
            value = float(raw)
        except ValueError:
            self.fail(name, f"must be a number, got {raw!r}")
            return float(default)
        if value < minimum:
            self.fail(name, f"must be at least {minimum}, got {raw!r}")
            return float(default)
        return value

    def integer(self, name: str, default: int | None, minimum: int | None, maximum: int | None = None) -> int | None:
        raw = self.raw(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            self.fail(name, f"must be an integer, got {raw!r}")
            return default
        if minimum is not None and value < minimum:
            self.fail(name, f"must be at least {minimum}, got {raw!r}")
        elif maximum is not None and value > maximum:
            self.fail(name, f"must be at most {maximum}, got {raw!r}")
        else:
            return value
        return default

    def choice(self, name: str, default: str, choices: tuple[str, ...], upper: bool = True) -> str:
        value = self.raw(name) or default
        value = value.upper() if upper else value.lower()
        if value not in choices:
            self.fail(name, f"must be one of {', '.join(choices)}, got {value!r}")
            return default
        return value

    def url(self, name: str, default: str, schemes: tuple[str, ...], secret: bool = False) -> str:
        value = self.raw(name) or default
        parts = urlsplit(value)
        shown = "" if secret else f", got {value!r}"  # never echo a URL that may hold a password
        if parts.scheme not in schemes or not parts.hostname:
            self.fail(name, f"must be a URL starting with {' or '.join(s + '://' for s in schemes)}{shown}")
            return default
        return value

    def clock_time(self, name: str, default: str) -> time:
        raw = self.raw(name) or default
        try:
            hours, minutes = raw.split(":")
            return time(int(hours), int(minutes))
        except ValueError:
            self.fail(name, f"must be a time of day as HH:MM, got {raw!r}")
            return time(8, 0)

    def probe_targets(self, name: str, default: str) -> tuple[tuple[str, int], ...]:
        raw = self.raw(name) or default
        targets = []
        for item in raw.split(","):
            host, _, port = item.strip().rpartition(":")
            if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
                self.fail(name, f"must be a comma-separated list of host:port, got {raw!r}")
                return ()
            targets.append((host, int(port)))
        return tuple(targets)

    def bot_token(self, name: str) -> str | None:
        """A Telegram token looks like '123456:ABC-def...'. The value itself is never echoed."""
        value = self.raw(name)
        if value is None:
            return None
        left, sep, right = value.partition(":")
        if not sep or not left.isdigit() or not right or any(ch.isspace() for ch in value):
            self.fail(name, "is not a valid Telegram bot token (expected '<digits>:<secret>')")
            return None
        return value
