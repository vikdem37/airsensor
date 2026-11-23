"""Collector: the poll loop that ties the data source, connectivity and tracker together.

The source is AlertsApi (JSON API, default) or Scraper (browser, backup). Both have the same
methods, described by the Source protocol below.

Every POLL_INTERVAL_S:
    read_state() ok      → connectivity.on_success() (closes any outage) → tracker.apply()
    read_state() raises  → log the attempt → connectivity.on_failure() (probe, open outage)
                         → wait with backoff 1, 2, 4 … BACKOFF_MAX_S (longer if Retry-After says so)
                         → next attempt calls load() (reloads the page; a no-op for the API), or
                           re-opens the source (new HTTP client / new browser) after every
                           BROWSER_RESTART_AFTER consecutive failures or after a browser crash
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from airsensor.config import Config
from airsensor.connectivity import Connectivity, utcnow
from airsensor.errors import ErrorKind, SourceError
from airsensor.logs import connectivity as log
from airsensor.tracker import Alert, Tracker


class Source(Protocol):
    @property
    def is_open(self) -> bool: ...
    async def open(self, reason: str) -> None: ...
    async def load(self) -> None: ...
    async def read_state(self) -> dict[str, Alert | None]: ...
    async def close(self) -> None: ...


def backoff_delay(attempt: int, max_s: float, retry_after_s: float | None = None) -> float:
    """Delay before retry number `attempt` (1-based): 1, 2, 4, 8 … capped at max_s, never below Retry-After."""
    delay = min(2.0 ** (attempt - 1), max_s)
    if retry_after_s is not None:
        delay = max(delay, retry_after_s)
    return delay


class Collector:
    def __init__(self, config: Config, scraper: Source, connectivity: Connectivity, tracker: Tracker) -> None:
        self._config = config
        self._scraper = scraper
        self._connectivity = connectivity
        self._tracker = tracker
        self.failures = 0  # consecutive failed attempts; 0 after a valid read

    async def run(self) -> None:
        while True:
            try:
                await self._prepare_page()
                states = await self._scraper.read_state()
            except SourceError as err:
                await self._on_failure(err)
                continue
            await self._on_success(states)
            await asyncio.sleep(self._config.poll_interval_s)

    async def _prepare_page(self) -> None:
        """Open the source if needed. After a failure: reload (browser), or re-open the source."""
        if not self._scraper.is_open:
            await self._scraper.open(reason="startup" if self.failures == 0 else "source not open after a failure")
        elif self.failures and self.failures % self._config.browser_restart_after == 0:
            await self._scraper.open(reason=f"{self.failures} consecutive failures")
        elif self.failures:
            await self._scraper.load()

    async def _on_failure(self, err: SourceError) -> None:
        self.failures += 1
        delay = backoff_delay(self.failures, self._config.backoff_max_s, err.retry_after_s)
        log.warning(
            "source_request_failed",
            f"Reading the alert source failed ({err.kind}: {err.reason}); attempt {self.failures}, next try in {delay:g}s",
            error_kind=err.kind.value, http_status=err.http_status, url=err.url, reason=err.reason,
            attempt=self.failures, next_retry_s=delay,
        )
        if await self._connectivity.on_failure(err):
            outage = self._connectivity.outage
            self._tracker.mark_unknown(outage.id if outage else None)
        if err.kind == ErrorKind.BROWSER_ERROR:
            await self._scraper.close()  # the next attempt starts a fresh browser
        await self._wait(delay)

    async def _on_success(self, states: dict[str, Alert | None]) -> None:
        now = utcnow()
        self.failures = 0
        await self._connectivity.on_success(now)
        await self._tracker.apply(states, now)
        log.debug(
            "source_poll_ok", "Read the alert source",
            alerts={raion: alert._asdict() for raion, alert in states.items() if alert},
        )

    async def _wait(self, delay: float) -> None:
        """Sleep for the backoff delay, but wake early if a background probe sees the internet return."""
        try:
            await asyncio.wait_for(self._connectivity.internet_back.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return
        self._connectivity.internet_back.clear()
        log.info("retry_early", "Internet probes succeed again; retrying the source now")
