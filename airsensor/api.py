"""HTTP API (read-only): /health, /alerts, /stats, /outages.

Times in query parameters are ISO 8601 dates or datetimes; values without a time zone are
read as Europe/Kyiv time. A date-only `to` includes that whole day. Times in responses are
shown in Europe/Kyiv time.

Error handling: bad parameters → 400 with a message. Database errors → 503 and `db_read_failed`.
Anything unexpected → 500 and `unhandled_exception` with the traceback.
"""

from __future__ import annotations

import contextlib
import time
from datetime import date, datetime, timedelta
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from airsensor import __version__
from airsensor.config import KYIV, RAIONS
from airsensor.connectivity import ConnState, Connectivity
from airsensor.db import DB_ERRORS, Database
from airsensor.logs import describe, events


def create_app(db: Database, connectivity: Connectivity, started_monotonic: float) -> FastAPI:
    app = FastAPI(title="AirSensor", version=__version__, description="Air-raid alerts in Lviv oblast by raion")

    @app.middleware("http")
    async def handle_errors(request: Request, call_next):
        try:
            return await call_next(request)
        except DB_ERRORS as err:
            events.error("db_read_failed", f"Database read failed for {request.url.path}",
                         path=request.url.path, error=describe(err))
            return JSONResponse({"error": "database_unavailable"}, status_code=503)
        except Exception:
            events.error("unhandled_exception", f"Unexpected error in {request.url.path}",
                         exc_info=True, task="http", path=request.url.path)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @app.get("/health")
    async def health() -> JSONResponse:
        """200 when the source is read successfully and the database answers; 503 otherwise."""
        db_up = await db.ping()
        outage = connectivity.outage
        internet = {None: "unknown", True: "up", False: "down"}[connectivity.internet_up]
        healthy = connectivity.state == ConnState.ONLINE and db_up
        body = {
            "status": "ok" if healthy else "degraded",
            "source": {
                "state": connectivity.state.value,
                "last_success_at": _show(connectivity.last_success_at),
                "outage": None if outage is None else {
                    "id": outage.id, "kind": outage.kind, "reason": outage.reason,
                    "started_at": _show(outage.started_at), "attempts": outage.attempts,
                },
            },
            "internet": {"state": internet, "last_probe_at": _show(connectivity.last_probe_at)},
            "database": "up" if db_up else "down",
            "uptime_s": round(time.monotonic() - started_monotonic),
            "version": __version__,
        }
        return JSONResponse(body, status_code=200 if healthy else 503)

    @app.get("/alerts")
    async def alerts(
        raion: str | None = None,
        alert_type: str | None = Query(None, alias="type"),
        from_: str | None = Query(None, alias="from"),
        to: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict[str, Any]:
        """Alert history, newest first: alerts active at any moment between `from` and `to`."""
        if raion is not None and raion not in RAIONS:
            raise HTTPException(400, f"raion must be one of: {', '.join(RAIONS)}")
        lo, hi = parse_time("from", from_), parse_time("to", to, end_of_day=True)
        rows = await db.list_alerts(raion, alert_type, lo, hi, limit)
        return {"count": len(rows), "alerts": [_alert(row) for row in rows]}

    @app.get("/stats")
    async def stats(from_: str | None = Query(None, alias="from"), to: str | None = None) -> dict[str, Any]:
        """Per raion: alert count, total duration and longest alert (only the part inside the window)."""
        lo, hi = parse_time("from", from_), parse_time("to", to, end_of_day=True)
        found = {row["raion"]: row for row in await db.alert_stats(lo, hi)}
        raions = []
        for raion in RAIONS:
            row = found.get(raion)
            raions.append({
                "raion": raion,
                "alert_count": row["alert_count"] if row else 0,
                "total_duration_s": round(row["total_s"]) if row else 0,
                "longest_alert_s": round(row["longest_s"]) if row else 0,
            })
        return {"from": _show(lo), "to": _show(hi), "raions": raions}

    @app.get("/outages")
    async def outages(
        from_: str | None = Query(None, alias="from"), to: str | None = None, limit: int = Query(100, ge=1, le=1000)
    ) -> dict[str, Any]:
        """The connection-failure log, newest first."""
        lo, hi = parse_time("from", from_), parse_time("to", to, end_of_day=True)
        rows = await db.list_outages(lo, hi, limit)
        return {"count": len(rows), "outages": [_outage(row) for row in rows]}

    return app


def parse_time(name: str, value: str | None, end_of_day: bool = False) -> datetime | None:
    """Parse an ISO 8601 date or datetime query parameter. Naive values are Kyiv time."""
    if not value:
        return None
    try:
        if len(value) == 10:  # a date such as 2026-11-18
            day = date.fromisoformat(value) + timedelta(days=1 if end_of_day else 0)
            return datetime.combine(day, datetime.min.time(), tzinfo=KYIV)
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"{name} must be an ISO 8601 date or datetime, e.g. 2026-11-18 or 2026-11-18T08:00")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=KYIV)


def _show(moment: datetime | None) -> str | None:
    return moment.astimezone(KYIV).isoformat(timespec="seconds") if moment else None


def _alert(row: Any) -> dict[str, Any]:
    end = row["ended_at"] or datetime.now(KYIV)
    return {
        "id": row["id"],
        "raion": row["raion"],
        "alert_type": row["alert_type"],
        "alert_level": row["alert_level"],
        "started_at": _show(row["started_at"]),
        "ended_at": _show(row["ended_at"]),
        "active": row["ended_at"] is None,
        "duration_s": round((end - row["started_at"]).total_seconds()),
        "start_uncertain": row["start_uncertain"],
        "end_uncertain": row["end_uncertain"],
    }


def _outage(row: Any) -> dict[str, Any]:
    end = row["ended_at"] or datetime.now(KYIV)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "reason": row["reason"],
        "started_at": _show(row["started_at"]),
        "ended_at": _show(row["ended_at"]),
        "ongoing": row["ended_at"] is None,
        "downtime_s": round((end - row["started_at"]).total_seconds()),
        "attempts": row["attempts"],
    }


class HttpServer(uvicorn.Server):
    """uvicorn, minus its own SIGINT/SIGTERM handling: main.py owns shutdown for the whole service."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    async def run_forever(self) -> None:
        try:
            await self.serve()
        except SystemExit as exit_:  # uvicorn exits the process if it cannot bind the port
            raise RuntimeError(f"HTTP server failed to start on port {self.config.port}") from exit_


def make_http_server(app: FastAPI, port: int) -> HttpServer:
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None, access_log=False, lifespan="off")
    return HttpServer(config)
