"""PostgreSQL access: connect with retry, create the schema, retried writes, read queries.

Error handling:
- At startup the database may not be up yet: connect() retries with backoff and logs
  `db_unavailable` on every failed attempt. A wrong password or database name is a
  configuration problem, not an outage, so it raises ConfigError instead.
- A write that fails with a connection-type error is retried 3 times (1, 2, 4 s).
  Any other database error is not retried. If the write still fails, `db_write_failed`
  is logged with the full record, so the log file is the backup copy of the data.
- Reads (HTTP API, report) are not retried: the caller turns a failure into a 503 or a
  failed report.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import asyncpg

from airsensor.config import ConfigError
from airsensor.logs import describe, events

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id              bigserial   PRIMARY KEY,
    raion           text        NOT NULL,
    alert_type      text        NOT NULL,
    started_at      timestamptz NOT NULL,
    ended_at        timestamptz,              -- NULL while the alert is active
    last_seen_at    timestamptz NOT NULL,     -- last valid read that showed this alert
    start_uncertain boolean     NOT NULL DEFAULT false,
    end_uncertain   boolean     NOT NULL DEFAULT false
);
-- Latest known level of the alert: 'red' or 'yellow'; NULL when the source did not report one.
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS alert_level text;
CREATE INDEX IF NOT EXISTS alerts_started_at_idx ON alerts (started_at);

CREATE TABLE IF NOT EXISTS outages (
    id         bigserial   PRIMARY KEY,
    kind       text        NOT NULL CHECK (kind IN ('source', 'internet', 'service')),
    reason     text        NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at   timestamptz,                   -- NULL while the outage is open
    attempts   integer     NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS outages_started_at_idx ON outages (started_at);
"""

# Errors that mean "the database is unreachable right now"; worth retrying.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    asyncio.TimeoutError,
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
    asyncpg.exceptions.AdminShutdownError,
)
# Any error a database call can raise; used by callers that turn it into a 503 or a failed report.
DB_ERRORS: tuple[type[BaseException], ...] = TRANSIENT_ERRORS + (asyncpg.PostgresError,)
# Errors that mean DATABASE_URL itself is wrong; retrying will not help.
CONFIG_ERRORS = (asyncpg.InvalidAuthorizationSpecificationError, asyncpg.InvalidCatalogNameError)

WRITE_RETRIES = 3
QUERY_TIMEOUT_S = 10

# Shared SQL: the time window [lo, hi) with NULL meaning "unbounded" / "now",
# and each row's interval clipped to that window.
_WINDOW = "SELECT COALESCE($1::timestamptz, '-infinity') AS lo, COALESCE($2::timestamptz, now()) AS hi"


class Database:
    def __init__(self, url: str) -> None:
        self._url = url
        self._pool: asyncpg.Pool | None = None

    # --- life cycle -------------------------------------------------------------

    async def connect(self, backoff_max_s: float) -> None:
        """Connect and create the schema, retrying with backoff until the database is reachable."""
        attempt = 0
        while True:
            attempt += 1
            try:
                self._pool = await asyncpg.create_pool(
                    self._url, min_size=1, max_size=5, timeout=QUERY_TIMEOUT_S, command_timeout=QUERY_TIMEOUT_S
                )
                await self._pool.execute(SCHEMA)
                return
            except CONFIG_ERRORS as err:
                await self.close()
                raise ConfigError([("DATABASE_URL", f"rejected by the database: {type(err).__name__}")]) from err
            except DB_ERRORS as err:
                await self.close()
                delay = min(2 ** (attempt - 1), backoff_max_s)
                events.error(
                    "db_unavailable",
                    f"Cannot connect to the database (attempt {attempt}); retrying in {delay:g}s",
                    attempt=attempt,
                    error=describe(err),
                    next_retry_s=delay,
                )
                await asyncio.sleep(delay)

    async def close(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            try:
                await asyncio.wait_for(pool.close(), timeout=5)
            except (asyncio.TimeoutError, *DB_ERRORS):
                pool.terminate()

    async def ping(self) -> bool:
        """True if the database answers a trivial query within 2 seconds."""
        if self._pool is None:
            return False
        try:
            await self._pool.fetchval("SELECT 1", timeout=2)
            return True
        except DB_ERRORS:
            return False

    # --- retried writes ---------------------------------------------------------

    async def _write(self, record: dict[str, Any], query: str, *args: Any) -> Any:
        """Run one write query. Returns its first value, or None if the write failed for good."""
        for attempt in range(1, WRITE_RETRIES + 2):
            try:
                if self._pool is None:
                    raise asyncpg.InterfaceError("database pool is not connected")
                return await self._pool.fetchval(query, *args)
            except TRANSIENT_ERRORS as err:
                if attempt <= WRITE_RETRIES:
                    delay = 2 ** (attempt - 1)
                    events.warning(
                        "db_write_retry",
                        f"Database write failed (attempt {attempt}); retrying in {delay}s",
                        attempt=attempt,
                        error=describe(err),
                        next_retry_s=delay,
                        record=record,
                    )
                    await asyncio.sleep(delay)
                    continue
                error = describe(err)
            except asyncpg.PostgresError as err:  # bad data or SQL: retrying would fail the same way
                error = describe(err)
            events.error(
                "db_write_failed",
                "Database write failed; the record is kept in this log line",
                attempt=attempt,
                error=error,
                record=record,
            )
            return None

    async def insert_alert(
        self, raion: str, alert_type: str, started_at: datetime, start_uncertain: bool, alert_level: str | None = None
    ) -> int | None:
        record = {"op": "insert_alert", "raion": raion, "alert_type": alert_type, "alert_level": alert_level,
                  "started_at": started_at, "start_uncertain": start_uncertain}
        return await self._write(
            record,
            "INSERT INTO alerts (raion, alert_type, alert_level, started_at, last_seen_at, start_uncertain) "
            "VALUES ($1, $2, $3, $4, $4, $5) RETURNING id",
            raion, alert_type, alert_level, started_at, start_uncertain,
        )

    async def set_alert_level(self, alert_id: int, alert_level: str) -> None:
        record = {"op": "set_alert_level", "alert_id": alert_id, "alert_level": alert_level}
        await self._write(record, "UPDATE alerts SET alert_level = $2 WHERE id = $1", alert_id, alert_level)

    async def end_alert(self, alert_id: int, ended_at: datetime, end_uncertain: bool) -> None:
        record = {"op": "end_alert", "alert_id": alert_id, "ended_at": ended_at, "end_uncertain": end_uncertain}
        await self._write(
            record,
            "UPDATE alerts SET ended_at = $2, end_uncertain = $3 WHERE id = $1",
            alert_id, ended_at, end_uncertain,
        )

    async def touch_alerts(self, alert_ids: list[int], seen_at: datetime) -> None:
        """Set last_seen_at on the open alerts that the latest valid read still showed."""
        record = {"op": "touch_alerts", "alert_ids": alert_ids, "seen_at": seen_at}
        await self._write(record, "UPDATE alerts SET last_seen_at = $2 WHERE id = ANY($1::bigint[])", alert_ids, seen_at)

    async def insert_outage(
        self, kind: str, reason: str, started_at: datetime, ended_at: datetime | None = None, attempts: int = 0
    ) -> int | None:
        record = {"op": "insert_outage", "kind": kind, "reason": reason,
                  "started_at": started_at, "ended_at": ended_at, "attempts": attempts}
        return await self._write(
            record,
            "INSERT INTO outages (kind, reason, started_at, ended_at, attempts) VALUES ($1, $2, $3, $4, $5) RETURNING id",
            kind, reason, started_at, ended_at, attempts,
        )

    async def close_outage(self, outage_id: int, ended_at: datetime, attempts: int) -> None:
        record = {"op": "close_outage", "outage_id": outage_id, "ended_at": ended_at, "attempts": attempts}
        await self._write(
            record,
            "UPDATE outages SET ended_at = $2, attempts = $3 WHERE id = $1",
            outage_id, ended_at, attempts,
        )

    # --- reads at startup -------------------------------------------------------

    async def open_alerts(self) -> list[asyncpg.Record]:
        return await self._fetch("SELECT * FROM alerts WHERE ended_at IS NULL ORDER BY started_at")

    async def open_outages(self) -> list[asyncpg.Record]:
        return await self._fetch("SELECT * FROM outages WHERE ended_at IS NULL ORDER BY started_at")

    # --- reads for the HTTP API and the report ------------------------------------

    async def list_alerts(
        self, raion: str | None, alert_type: str | None, lo: datetime | None, hi: datetime | None, limit: int
    ) -> list[asyncpg.Record]:
        """Alerts that were active at any moment in [lo, hi), newest first."""
        return await self._fetch(
            f"WITH win AS ({_WINDOW}) "
            "SELECT a.* FROM alerts a, win "
            "WHERE a.started_at < win.hi AND COALESCE(a.ended_at, now()) > win.lo "
            "AND ($3::text IS NULL OR a.raion = $3) AND ($4::text IS NULL OR a.alert_type = $4) "
            "ORDER BY a.started_at DESC LIMIT $5",
            lo, hi, raion, alert_type, limit,
        )

    async def alert_stats(self, lo: datetime | None, hi: datetime | None) -> list[asyncpg.Record]:
        """Per raion: alert count, total and longest duration. Only the part inside [lo, hi) counts."""
        return await self._fetch(
            f"WITH win AS ({_WINDOW}), clipped AS ("
            "  SELECT a.raion, GREATEST(a.started_at, win.lo) AS s, LEAST(COALESCE(a.ended_at, now()), win.hi) AS e"
            "  FROM alerts a, win WHERE a.started_at < win.hi AND COALESCE(a.ended_at, now()) > win.lo) "
            "SELECT raion, count(*) AS alert_count, "
            "       sum(EXTRACT(EPOCH FROM e - s))::float8 AS total_s, "
            "       max(EXTRACT(EPOCH FROM e - s))::float8 AS longest_s "
            "FROM clipped GROUP BY raion",
            lo, hi,
        )

    async def list_outages(self, lo: datetime | None, hi: datetime | None, limit: int) -> list[asyncpg.Record]:
        """Outages that overlap [lo, hi), newest first."""
        return await self._fetch(
            f"WITH win AS ({_WINDOW}) "
            "SELECT o.* FROM outages o, win "
            "WHERE o.started_at < win.hi AND COALESCE(o.ended_at, now()) > win.lo "
            "ORDER BY o.started_at DESC LIMIT $3",
            lo, hi, limit,
        )

    async def outage_stats(self, lo: datetime | None, hi: datetime | None) -> list[asyncpg.Record]:
        """Per outage kind: count and total downtime inside [lo, hi)."""
        return await self._fetch(
            f"WITH win AS ({_WINDOW}), clipped AS ("
            "  SELECT o.kind, GREATEST(o.started_at, win.lo) AS s, LEAST(COALESCE(o.ended_at, now()), win.hi) AS e"
            "  FROM outages o, win WHERE o.started_at < win.hi AND COALESCE(o.ended_at, now()) > win.lo) "
            "SELECT kind, count(*) AS outage_count, sum(EXTRACT(EPOCH FROM e - s))::float8 AS total_s "
            "FROM clipped GROUP BY kind",
            lo, hi,
        )

    async def _fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        if self._pool is None:
            raise asyncpg.InterfaceError("database pool is not connected")
        return await self._pool.fetch(query, *args)
