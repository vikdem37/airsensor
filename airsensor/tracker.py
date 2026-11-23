"""Tracker: one small state machine per raion — CLEAR, ALERT(type) or UNKNOWN.

Rules:
- A valid read moves each raion to CLEAR or ALERT(type) and writes alert rows when that changes.
- The alert level (red / yellow) is an attribute of an alert, not a new alert: a level change
  during an alert updates the open row and logs `alert_level_changed`. A level of None means
  the source does not report one (browser mode); it never overwrites a known level.
- While an outage is open every raion is UNKNOWN. An alert is never started or ended just
  because data stopped arriving: "no data" is not an all-clear.
- The first valid read after UNKNOWN compares each raion with its state before the outage:
    same alert still active  → the alert row stays open (the outage row records the gap)
    alert gone               → end it now, with end_uncertain = true
    new alert                → start it now, with start_uncertain = true
- On startup every raion is UNKNOWN until the first valid read. Alert rows left open by the
  previous run are loaded as the "before" state, and the gap since they were last seen is
  recorded as an outage of kind `service`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from airsensor.config import KNOWN_ALERT_TYPES, RAIONS
from airsensor.db import Database
from airsensor.logs import events


class Alert(NamedTuple):
    """An active alert in one raion, as a source reports it. No alert is represented by None."""

    type: str            # e.g. "air-raid", "artillery-shelling"
    level: str | None    # "red", "yellow", or None when the source does not say


@dataclass
class RaionState:
    alert_type: str | None = None       # last known state: None = clear
    alert_level: str | None = None      # level of the open alert, if known
    alert_id: int | None = None         # id of the open row in `alerts` (None if not stored)
    started_at: datetime | None = None  # when the open alert started
    unknown: bool = True                # True until the first valid read, and during outages


class Tracker:
    def __init__(self, db: Database) -> None:
        self._db = db
        self.raions: dict[str, RaionState] = {raion: RaionState() for raion in RAIONS}
        self._unknown_outage_id: int | None = None  # outage that made the states unknown, for the log
        self._unknown_logged = False

    async def restore(self, now: datetime) -> None:
        """Startup: load alerts left open by the previous run and record the downtime as a `service` outage."""
        rows = await self._db.open_alerts()
        for row in rows:
            state = self.raions.get(row["raion"])
            if state is None or state.alert_type is not None:
                # Not one of our raions, or a second open row for one: close it rather than track it twice.
                await self._db.end_alert(row["id"], now, end_uncertain=True)
                events.warning(
                    "stale_alert_closed", f"Closed an unexpected open alert row for {row['raion']}",
                    alert_id=row["id"], raion=row["raion"], alert_type=row["alert_type"],
                )
                continue
            state.alert_type = row["alert_type"]
            state.alert_level = row.get("alert_level")  # asyncpg.Record has .get() too
            state.alert_id = row["id"]
            state.started_at = row["started_at"]
        if not rows:
            return
        last_seen = max(row["last_seen_at"] for row in rows)
        outage_id = await self._db.insert_outage("service", "service_not_running", last_seen, ended_at=now)
        gap_s = round((now - last_seen).total_seconds())
        events.warning(
            "service_downtime_recorded",
            f"The service was not running for {gap_s}s while alerts were open",
            gap_s=gap_s, outage_id=outage_id, open_alerts=self._open_alerts(),
        )
        self.mark_unknown(outage_id)

    def mark_unknown(self, outage_id: int | None) -> None:
        """An outage started: every raion's state is unknown until the next valid read."""
        for state in self.raions.values():
            state.unknown = True
        self._unknown_outage_id = outage_id
        self._unknown_logged = True
        events.warning(
            "alert_state_unknown",
            "No valid data: all raions are in state unknown (not all-clear)",
            raions=list(RAIONS), open_alerts=self._open_alerts(), outage_id=outage_id,
        )

    async def apply(self, new_states: dict[str, Alert | None], now: datetime) -> None:
        """Apply one valid read: start, end or keep alerts, and leave the unknown state."""
        for raion, state in self.raions.items():
            new = new_states[raion]
            new_type = new.type if new else None
            uncertain = state.unknown  # we did not watch the change happen
            state.unknown = False
            if new_type == state.alert_type:
                if new is not None and new.level is not None and new.level != state.alert_level:
                    await self._change_level(raion, state, new.level)
                continue
            if state.alert_type is not None:
                await self._end(raion, state, now, end_uncertain=uncertain)
            if new is not None:
                await self._start(raion, state, new, now, start_uncertain=uncertain)

        if self._unknown_logged:
            events.info(
                "alert_state_restored", "Valid data again: raion states are known",
                raions=list(RAIONS), open_alerts=self._open_alerts(), outage_id=self._unknown_outage_id,
            )
            self._unknown_logged = False
            self._unknown_outage_id = None

        open_ids = [state.alert_id for state in self.raions.values() if state.alert_id is not None]
        if open_ids:
            await self._db.touch_alerts(open_ids, now)

    # --- internals ----------------------------------------------------------------

    async def _start(self, raion: str, state: RaionState, alert: Alert, now: datetime, start_uncertain: bool) -> None:
        alert_type, level = alert
        if alert_type not in KNOWN_ALERT_TYPES:
            events.warning(
                "unknown_alert_type", f"Unknown alert type {alert_type!r} in {raion}; stored as it is",
                raion=raion, value=alert_type,
            )
        state.alert_type, state.alert_level = alert_type, level
        state.started_at = now
        state.alert_id = await self._db.insert_alert(raion, alert_type, now, start_uncertain, level)
        events.info(
            "alert_started", f"Alert ({alert_type}) started in {raion}",
            raion=raion, alert_type=alert_type, alert_level=level, started_at=now, start_uncertain=start_uncertain,
            alert_id=state.alert_id,
        )

    async def _change_level(self, raion: str, state: RaionState, level: str) -> None:
        previous, state.alert_level = state.alert_level, level
        if state.alert_id is not None:
            await self._db.set_alert_level(state.alert_id, level)
        events.info(
            "alert_level_changed", f"Alert ({state.alert_type}) in {raion} changed level from {previous} to {level}",
            raion=raion, alert_type=state.alert_type, previous_level=previous, alert_level=level,
            alert_id=state.alert_id,
        )

    async def _end(self, raion: str, state: RaionState, now: datetime, end_uncertain: bool) -> None:
        alert_type, alert_id = state.alert_type, state.alert_id
        duration_s = round((now - state.started_at).total_seconds()) if state.started_at else None
        if alert_id is not None:
            await self._db.end_alert(alert_id, now, end_uncertain)
        else:
            events.error(
                "db_write_failed", f"Cannot store the end of the alert in {raion}: its start was never stored",
                record={"op": "end_alert", "raion": raion, "alert_type": alert_type,
                        "started_at": state.started_at, "ended_at": now, "end_uncertain": end_uncertain},
            )
        state.alert_type = state.alert_level = state.alert_id = state.started_at = None
        events.info(
            "alert_ended", f"Alert ({alert_type}) ended in {raion} after {duration_s}s",
            raion=raion, alert_type=alert_type, duration_s=duration_s, end_uncertain=end_uncertain, alert_id=alert_id,
        )

    def _open_alerts(self) -> dict[str, str]:
        return {raion: state.alert_type for raion, state in self.raions.items() if state.alert_type is not None}
