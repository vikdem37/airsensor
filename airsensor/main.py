"""Entry point: configuration, start-up, task supervision and graceful shutdown.

Start-up:
  1. Read and validate the configuration. Any problem → `config_error` per field, exit code 2.
  2. Connect to PostgreSQL (retry with backoff) and create the schema.
  3. Close outages that a crashed previous run left open; restore open alerts and record
     the service downtime.
  4. Start the long-running tasks, each under a supervisor that logs `unhandled_exception`
     with the traceback and restarts it: collector, internet probe, HTTP API, daily report.

Shutdown (SIGTERM from `docker compose stop`, or Ctrl+C): `service_stopping`, stop the tasks,
close an open outage, close the source (HTTP client or browser) and the database, `service_stopped`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from functools import partial

from airsensor import __version__
from airsensor.alerts_api import AlertsApi
from airsensor.api import create_app, make_http_server
from airsensor.collector import Collector, Source
from airsensor.config import Config, ConfigError, load_config
from airsensor.connectivity import Connectivity, close_stale_outages, utcnow
from airsensor.db import Database
from airsensor.logs import events, register_secret, setup_logging
from airsensor.report import report_loop
from airsensor.tracker import Tracker

TASK_RESTART_MAX_S = 60


def make_source(config: Config) -> Source:
    """SOURCE_MODE=api (default): JSON API. SOURCE_MODE=browser: the Playwright scraper (backup)."""
    if config.source_mode == "browser":
        from airsensor.scraper import Scraper  # imported only here, so api mode does not need Playwright
        return Scraper(config)
    return AlertsApi(config)


def load_config_or_exit() -> Config:
    """Load the configuration and set up logging. On any configuration problem, log it and exit with code 2."""
    try:
        config = load_config(os.environ)
    except ConfigError as err:
        setup_logging(os.environ.get("LOG_DIR") or "logs")
        log_config_errors(err)
        sys.exit(2)
    setup_logging(config.log_dir, config.log_level)
    register_secret(config.bot_token)
    return config


def log_config_errors(err: ConfigError) -> None:
    for field, reason in err.problems:
        events.error("config_error", f"Invalid setting {field}: {reason}", field=field, reason=reason)


async def supervise(name: str, job: Callable[[], Awaitable[None]], stopping: asyncio.Event) -> None:
    """Run a long-running job; if it crashes (or returns), log it and start it again with backoff."""
    delay = 1.0
    while not stopping.is_set():
        started = time.monotonic()
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception:
            if time.monotonic() - started > TASK_RESTART_MAX_S:
                delay = 1.0  # it ran fine for a while: start the backoff again
            events.error("unhandled_exception", f"Task {name} crashed; restarting it in {delay:g}s",
                         exc_info=True, task=name, restart_in_s=delay)
        else:
            if stopping.is_set():
                return
            events.error("unhandled_exception", f"Task {name} returned unexpectedly; restarting it in {delay:g}s",
                         task=name, restart_in_s=delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, TASK_RESTART_MAX_S)


async def run(config: Config) -> int:
    """Run the service until a stop signal. Returns the process exit code."""
    started = time.monotonic()
    stopping = asyncio.Event()
    received: list[str] = []
    main_task = asyncio.current_task()
    assert main_task is not None

    def request_stop(signal_name: str) -> None:
        if received:  # already stopping; a second signal must not interrupt the shutdown
            return
        received.append(signal_name)
        main_task.cancel()

    _install_signal_handlers(request_stop)
    events.info("service_started", f"AirSensor {__version__} started", version=__version__, config=config.masked())

    db = Database(config.database_url)
    scraper = make_source(config)
    connectivity = Connectivity(config, db)
    server = None
    tasks: list[asyncio.Task[None]] = []
    exit_code = 0
    try:
        await db.connect(config.backoff_max_s)
        await close_stale_outages(db)
        tracker = Tracker(db)
        await tracker.restore(utcnow())
        collector = Collector(config, scraper, connectivity, tracker)
        server = make_http_server(create_app(db, connectivity, started), config.http_port)

        jobs: dict[str, Callable[[], Awaitable[None]]] = {
            "collector": collector.run,
            "internet_probe": connectivity.probe_loop,
            "http": server.run_forever,
        }
        if config.report_enabled:
            jobs["daily_report"] = partial(report_loop, config, db)
        else:
            events.info("report_disabled", "Daily report is off: BOT_TOKEN and ADMIN_CHAT_ID are not set")
        tasks = [asyncio.create_task(supervise(name, job, stopping), name=name) for name, job in jobs.items()]
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        main_task.uncancel()  # the cancel was our own stop request; carry on with the shutdown
    except ConfigError as err:  # e.g. the database rejected the credentials in DATABASE_URL
        log_config_errors(err)
        exit_code = 2
    except Exception:
        events.error("unhandled_exception", "Start-up failed", exc_info=True, task="startup")
        exit_code = 1

    events.info("service_stopping", "AirSensor is stopping",
                signal=received[0] if received else None, uptime_s=round(time.monotonic() - started))
    stopping.set()
    if server is not None:
        server.should_exit = True
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await connectivity.interrupt_outage("service stopped")
    await scraper.close()
    await db.close()
    events.info("service_stopped", "AirSensor stopped", uptime_s=round(time.monotonic() - started), exit_code=exit_code)
    return exit_code


def _install_signal_handlers(request_stop: Callable[[str], None]) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop, sig.name)
        except NotImplementedError:  # Windows: no loop signal handlers; fall back to signal.signal
            signal.signal(sig, lambda signum, _frame: loop.call_soon_threadsafe(request_stop, signal.Signals(signum).name))


def main() -> None:
    config = load_config_or_exit()
    sys.exit(asyncio.run(run(config)))


if __name__ == "__main__":
    main()
