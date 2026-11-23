"""Connectivity: internet probes, the connection state and outages.

The connection state is one of:
    starting       no valid read yet since the service started
    online         the last read of the source was valid
    source_down    the last read failed, but at least one internet probe succeeded
    internet_down  the last read failed, and every internet probe failed too

An outage is the time between the first failed read and the next valid read.
- The first failure opens one outage (a database row and a log line); later failures
  only increase its attempt count.
- If the cause changes during an outage (internet lost while the source was already down,
  or internet back while the source still fails), the current outage is closed and one of
  the new kind is opened right away, so the timeline has no gaps.
- The first valid read closes the outage and logs its downtime and attempt count.

Probes are TCP connections to independent addresses (8.8.8.8:53, 1.1.1.1:53 by default);
unlike ICMP ping they need no extra privileges inside Docker.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from airsensor.config import Config
from airsensor.db import Database
from airsensor.errors import SourceError
from airsensor.logs import connectivity as log


class ConnState(StrEnum):
    STARTING = "starting"
    ONLINE = "online"
    SOURCE_DOWN = "source_down"
    INTERNET_DOWN = "internet_down"


@dataclass
class Outage:
    id: int | None  # None if the database write failed (the log line still has it)
    kind: str       # "source" or "internet"
    reason: str     # error_kind of the failure that opened it
    started_at: datetime
    attempts: int = 0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def probe_target(host: str, port: int, timeout_s: float) -> str | None:
    """Try a TCP connection. Returns None on success, or a short description of the failure."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
    except asyncio.TimeoutError:
        return f"no answer within {timeout_s:g}s"
    except OSError as err:
        return f"{type(err).__name__}: {err.strerror or err}"
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass  # the probe already succeeded; a failed close does not matter
    return None


class Connectivity:
    def __init__(self, config: Config, db: Database) -> None:
        self._config = config
        self._db = db
        self.state = ConnState.STARTING
        self.internet_up: bool | None = None
        self.last_probe_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.outage: Outage | None = None
        self.internet_back = asyncio.Event()  # wakes the collector early when the internet returns
        self._target_ok: dict[str, bool] = {}

    async def check_internet(self) -> bool:
        """Probe every target at once. True if at least one answered."""
        targets = self._config.probe_targets
        results = await asyncio.gather(*(probe_target(h, p, self._config.probe_timeout_s) for h, p in targets))
        for (host, port), error in zip(targets, results):
            target = f"{host}:{port}"
            if error is not None:
                # Warn when a target goes from working to failing; repeats are DEBUG to keep the log readable.
                level = log.warning if self._target_ok.get(target, True) else log.debug
                level("internet_probe_failed", f"Internet probe to {target} failed: {error}", target=target, error=error)
            self._target_ok[target] = error is None
        up = any(error is None for error in results)
        self.internet_up = up
        self.last_probe_at = utcnow()
        if up and self.state == ConnState.INTERNET_DOWN:
            self.internet_back.set()
        return up

    async def probe_loop(self) -> None:
        """Background task: keep the internet state fresh for /health and for early retries."""
        while True:
            await self.check_internet()
            await asyncio.sleep(self._config.probe_interval_s)

    async def on_failure(self, err: SourceError) -> bool:
        """Record a failed read. Returns True if a new outage was opened (raion states become unknown)."""
        kind = "source" if await self.check_internet() else "internet"
        now = utcnow()
        opened = False
        if self.outage is None:
            await self._open(kind, err, now)
            opened = True
        elif self.outage.kind != kind:
            await self._switch(kind, err, now)
        assert self.outage is not None
        self.outage.attempts += 1
        self.state = ConnState.SOURCE_DOWN if kind == "source" else ConnState.INTERNET_DOWN
        self.internet_back.clear()  # only a later background probe may cut the backoff short
        return opened

    async def on_success(self, now: datetime) -> Outage | None:
        """Record a valid read. Returns the outage it ended, if there was one."""
        self.last_success_at = now
        self.state = ConnState.ONLINE
        ended, self.outage = self.outage, None
        self.internet_back.clear()
        if ended is not None:
            await self._close(ended, now)
        return ended

    async def interrupt_outage(self, reason: str) -> None:
        """At shutdown: close the open outage, because nothing is observed after this moment."""
        if self.outage is None:
            return
        outage, self.outage = self.outage, None
        now = utcnow()
        if outage.id is not None:
            await self._db.close_outage(outage.id, now, outage.attempts)
        log.warning(
            "outage_interrupted",
            f"Outage {outage.id} closed without recovery: {reason}",
            outage_id=outage.id, kind=outage.kind, downtime_s=_seconds(now - outage.started_at), reason=reason,
        )

    # --- internals ----------------------------------------------------------------

    async def _open(self, kind: str, err: SourceError, now: datetime, replaces: int | None = None) -> None:
        outage_id = await self._db.insert_outage(kind, err.kind.value, now)
        self.outage = Outage(id=outage_id, kind=kind, reason=err.kind.value, started_at=now)
        fields = {"outage_id": outage_id, "error_kind": err.kind.value, "http_status": err.http_status,
                  "reason": err.reason}
        if replaces is not None:
            fields["previous_outage_id"] = replaces
        if kind == "source":
            log.error("source_unavailable", f"The alert map is unavailable ({err.kind}); internet is up", **fields)
        else:
            log.error("internet_lost", "Internet connection lost: the source and all internet probes fail", **fields)

    async def _close(self, outage: Outage, now: datetime) -> None:
        if outage.id is not None:
            await self._db.close_outage(outage.id, now, outage.attempts)
        downtime_s = _seconds(now - outage.started_at)
        fields = {"outage_id": outage.id, "downtime_s": downtime_s, "attempts": outage.attempts}
        if outage.kind == "source":
            log.info("source_recovered", f"The alert map is available again after {downtime_s}s", **fields)
        else:
            log.info("internet_restored", f"Internet connection restored after {downtime_s}s", **fields)

    async def _switch(self, kind: str, err: SourceError, now: datetime) -> None:
        """The cause of an ongoing outage changed: close it and open one of the new kind."""
        old = self.outage
        assert old is not None
        if old.kind == "internet":
            await self._close(old, now)  # logs internet_restored; the source is still failing
        elif old.id is not None:
            await self._db.close_outage(old.id, now, old.attempts)
        await self._open(kind, err, now, replaces=old.id)


async def close_stale_outages(db: Database) -> None:
    """Startup: a previous run that crashed may have left outages open. Close them now.

    Their real end is unknown (nothing was observed after the crash), so they end at startup time.
    """
    now = utcnow()
    for row in await db.open_outages():
        await db.close_outage(row["id"], now, row["attempts"])
        log.warning(
            "outage_interrupted",
            f"Outage {row['id']} was left open by the previous run; closed at startup",
            outage_id=row["id"], kind=row["kind"], downtime_s=_seconds(now - row["started_at"]),
            reason="left open by the previous run",
        )


def _seconds(delta: timedelta) -> int:
    return round(delta.total_seconds())
