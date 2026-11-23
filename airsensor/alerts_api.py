"""AlertsApi (default source, SOURCE_MODE=api): reads alerts straight from the map's JSON backend.

The alerts.in.ua map loads its data from `api.alerts.in.ua/v3/alerts/active.*`. The `.json`
variant is plain JSON, public, and needs no token, cookie or API key (checked 2026-11-19), so
there is nothing to log in to and nothing to refresh. One GET per poll replaces the browser.
The API says about itself: "This API is private and may change anytime". That is why every
response is validated, and why the browser scraper is kept as a backup (SOURCE_MODE=browser).

Response format ("compact", decoded from the site's own code and checked against the live map):

    {"alerts": [ {...}, ... ], "meta": {"last_updated_at": <ms>, "type": "compact"}, ...}

    t      location type: o = oblast, r = raion, h = hromada, c = city
    luid   location uid (the official alerts.in.ua uid: 27 = Lviv oblast, 88-94 = its raions)
    n/ni   location name, given directly (n) or as an index into locations.json (ni)
    loi    oblast, as an index into locations.json (19 = Львівська область)
    lruid  raion uid, for hromada and city records
    at     alert type code: missing/0 = air-raid, 1 = artillery-shelling, 2 = urban-fights, 6 = info
    al     alert level: 1 = yellow (drones), missing = red (missiles / mass strike)
    s/u/f  started / updated / finished, in seconds since 1640000000 (not since 1970)

`fetch_alerts()` returns the whole country as a list of FeedAlert. `read_state()` turns that
into {raion: Alert or None} for the 7 Lviv raions, which is all the rest of the service uses.

Failure classification (same ErrorKind vocabulary as the scraper):
    httpx timeout                         → timeout
    httpx transport error (DNS, refused)  → network_error
    HTTP 5xx / 429 / other 4xx            → http_5xx / http_429 (+Retry-After) / invalid_response
    Cloudflare challenge                  → invalid_response, reason bot_check
    not JSON, or not the expected shape   → invalid_response
    the CDN copy is older than STALE_AFTER_S (HTTP Age header) → stale_data
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from airsensor import __version__
from airsensor.config import OBLAST_UID, RAION_UIDS, Config, load_config
from airsensor.errors import ErrorKind, SourceError, error_for_status
from airsensor.logs import connectivity, describe
from airsensor.tracker import Alert

# please note that the wall of text above will be processed
# huge comments below too

API_EPOCH_S = 1_640_000_000
LOCATION_TYPES = {"o": "oblast", "r": "raion", "h": "hromada", "c": "city"}
# Codes 3, 4, 5, 90 and 99 exist in the site's code, but their names are not confirmed.
# They are stored as "at-<code>", which the tracker logs as unknown_alert_type.
ALERT_TYPES = {0: "air-raid", 1: "artillery-shelling", 2: "urban-fights", 6: "info"}
NOT_AN_ALERT = frozenset({"info"})  # informational markers on the map, not alerts
# When one raion has several records at once, the first type in this list wins.
TYPE_PRIORITY = ("air-raid", "urban-fights", "artillery-shelling", "chemical", "nuclear")


@dataclass(frozen=True)
class FeedAlert:
    """One active record from the API, decoded into readable field names."""

    id: int
    location_uid: int
    location_type: str          # oblast, raion, hromada, city (or the raw code if unknown)
    location_title: str | None  # None if the record has only `ni` and locations.json is not loaded
    oblast_title: str | None
    raion_uid: int | None
    alert_type: str
    alert_level: str            # red or yellow
    started_at: datetime | None
    updated_at: datetime | None


class AlertsApi:
    """Polls the JSON API. Same interface as Scraper, so Collector works with either."""

    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._transport = transport  # tests pass an httpx.MockTransport; None = the real network
        self._client: httpx.AsyncClient | None = None
        self._names: list[str] | None = None  # locations.json titles, for `ni` and `loi`
        self._starts = 0

    @property
    def is_open(self) -> bool:
        return self._client is not None and not self._client.is_closed

    async def open(self, reason: str) -> None:
        """Create (or re-create) the HTTP client. Re-creating it drops all pooled connections."""
        await self.close()
        self._starts += 1
        self._client = httpx.AsyncClient(
            transport=self._transport,
            timeout=self._config.request_timeout_s,
            headers={"User-Agent": f"AirSensor/{__version__}", "Accept": "application/json"},
        )
        if self._starts == 1:
            connectivity.info("source_client_started", "HTTP client for the alerts API started", reason=reason)
        else:
            connectivity.warning("source_client_restarted", f"HTTP client for the alerts API restarted: {reason}",
                                 reason=reason)
        if self._names is None:
            await self._load_names()

    async def load(self) -> None:
        """Nothing to reload: every read is a fresh request. Kept so Collector can treat both sources alike."""

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception as err:  # cleanup only 
                connectivity.debug("source_client_close_failed", "Closing the HTTP client failed", error=describe(err))

    async def fetch_alerts(self) -> list[FeedAlert]:
        """All active alerts in the country, as returned by the API. Raises SourceError."""
        payload = await self._get_json(self._config.api_url, self._config.stale_after_s)
        return decode_feed(payload, self._names)

    async def read_state(self) -> dict[str, Alert | None]:
        """Return {raion: Alert or None} for all 7 Lviv raions, or raise SourceError."""
        return lviv_states(await self.fetch_alerts())

    # --- internals ----------------------------------------------------------------

    async def _get_json(self, url: str, stale_after_s: float | None = None) -> Any:
        """GET and decode JSON. `stale_after_s` checks the CDN's Age header (live data only, not static files)."""
        if self._client is None:
            raise SourceError(ErrorKind.NETWORK_ERROR, "the HTTP client is not open", url=url)
        try:
            response = await self._client.get(url)
        except httpx.TimeoutException as err:
            raise SourceError(ErrorKind.TIMEOUT, f"no answer within {self._config.request_timeout_s:g}s "
                              f"({type(err).__name__})", url=url) from err
        except httpx.RequestError as err:
            raise SourceError(ErrorKind.NETWORK_ERROR, describe(err), url=url) from err
        return parse_response(response.status_code, response.headers, response.text, url, stale_after_s)

    async def _load_names(self) -> None:
        """Location titles are only for readable output; the Lviv state needs uids only. Failure is not fatal."""
        url = self._config.locations_url
        try:
            data = await self._get_json(url)
            if not isinstance(data, list) or not all(isinstance(item, dict) and "title" in item for item in data):
                raise SourceError(ErrorKind.INVALID_RESPONSE, "locations.json is not a list of {title, type}", url=url)
        except SourceError as err:
            connectivity.warning("locations_unavailable", f"Location names not loaded ({err.kind}: {err.reason}); "
                                 "records given only by index will have no title", error_kind=err.kind.value,
                                 reason=err.reason, url=url)
            return
        self._names = [item["title"] for item in data]


# --- pure helpers (unit-tested without the network) -----------------------------------


def parse_response(status: int, headers: Mapping[str, str], text: str, url: str, stale_after_s: float | None) -> Any:
    """Check the status, the bot check, staleness (if stale_after_s is given) and JSON syntax. Returns the JSON."""
    error = error_for_status(status, url, headers)
    if error:
        raise error
    content_type = headers.get("content-type", "")
    if "html" in content_type:
        reason = "bot_check" if "Just a moment" in text or "challenge" in text else "got an HTML page instead of JSON"
        raise SourceError(ErrorKind.INVALID_RESPONSE, reason, http_status=status, url=url)
    age = headers.get("age", "")
    if stale_after_s is not None and age.isdigit() and int(age) > stale_after_s:
        raise SourceError(ErrorKind.STALE_DATA, f"the cached response is {age}s old", http_status=status, url=url)
    try:
        return json.loads(text)
    except ValueError as err:
        raise SourceError(ErrorKind.INVALID_RESPONSE, f"response is not valid JSON: {err}", http_status=status,
                          url=url) from err


def decode_feed(payload: Any, names: list[str] | None = None) -> list[FeedAlert]:
    """Turn the compact API payload into FeedAlert records. Any unexpected shape fails loudly."""
    if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
        raise SourceError(ErrorKind.INVALID_RESPONSE, "response has no 'alerts' list")
    meta = payload.get("meta")
    if not isinstance(meta, dict) or not isinstance(meta.get("last_updated_at"), int):
        raise SourceError(ErrorKind.INVALID_RESPONSE, "response has no meta.last_updated_at")
    return [_decode_item(index, item, names) for index, item in enumerate(payload["alerts"])]


def lviv_states(alerts: list[FeedAlert]) -> dict[str, Alert | None]:
    """Reduce the country-wide list to {raion: Alert or None} for the 7 Lviv raions.

    An oblast-level alert covers every raion. Hromada and city records are out of scope.
    Raions without a record are clear: the API lists active alerts only.
    """
    found: dict[str, list[Alert]] = {raion: [] for raion in RAION_UIDS.values()}
    oblast: list[Alert] = []
    for item in alerts:
        if item.alert_type in NOT_AN_ALERT:
            continue
        alert = Alert(item.alert_type, item.alert_level)
        if item.location_type == "oblast" and item.location_uid == OBLAST_UID:
            oblast.append(alert)
        elif item.location_type == "raion" and item.location_uid in RAION_UIDS:
            found[RAION_UIDS[item.location_uid]].append(alert)
    return {raion: _strongest(own or oblast) for raion, own in found.items()}


def _strongest(alerts: list[Alert]) -> Alert | None:
    """Pick one alert when there are several: by TYPE_PRIORITY, then red before yellow."""
    if not alerts:
        return None
    def rank(alert: Alert) -> tuple[int, int]:
        type_rank = TYPE_PRIORITY.index(alert.type) if alert.type in TYPE_PRIORITY else len(TYPE_PRIORITY)
        return type_rank, 0 if alert.level == "red" else 1
    return min(alerts, key=rank)


def _decode_item(index: int, item: Any, names: list[str] | None) -> FeedAlert:
    if not isinstance(item, dict):
        raise SourceError(ErrorKind.INVALID_RESPONSE, f"alerts[{index}] is not an object")
    for field in ("i", "luid", "t"):
        if field not in item:
            raise SourceError(ErrorKind.INVALID_RESPONSE, f"alerts[{index}] has no '{field}' field")
    if not isinstance(item["luid"], int) or not isinstance(item["t"], str):
        raise SourceError(ErrorKind.INVALID_RESPONSE, f"alerts[{index}] has a malformed 'luid' or 't'")
    code = item.get("at") or 0
    return FeedAlert(
        id=item["i"],
        location_uid=item["luid"],
        location_type=LOCATION_TYPES.get(item["t"], item["t"]),
        location_title=item.get("n") or _name(names, item.get("ni")),
        oblast_title=_name(names, item.get("loi")),
        raion_uid=item.get("lruid"),
        alert_type=ALERT_TYPES.get(code, f"at-{code}"),
        alert_level="yellow" if item.get("al") == 1 else "red",
        started_at=_time(item.get("s")),
        updated_at=_time(item.get("u")),
    )


def _name(names: list[str] | None, index: Any) -> str | None:
    if names is None or not isinstance(index, int) or not 0 <= index < len(names):
        return None
    return names[index]


def _time(value: Any) -> datetime | None:
    if not isinstance(value, int):
        return None
    return datetime.fromtimestamp(API_EPOCH_S + value, tz=timezone.utc)


# --- command line: print the current data as a JSON array ----------------------------


async def _cli(lviv_only: bool) -> int:
    api = AlertsApi(load_config(os.environ))
    try:
        await api.open(reason="command line")
        alerts = await api.fetch_alerts()
    except SourceError as err:
        print(f"error: {err.kind}: {err.reason}", file=sys.stderr)
        return 1
    finally:
        await api.close()
    if lviv_only:
        output: Any = {raion: alert._asdict() if alert else None for raion, alert in lviv_states(alerts).items()}
    else:
        output = [asdict(alert) for alert in alerts]
    print(json.dumps(output, ensure_ascii=False, indent=1, default=str))
    return 0


def main() -> None:
    """python -m airsensor.alerts_api [--lviv]"""
    sys.exit(asyncio.run(_cli("--lviv" in sys.argv[1:])))


if __name__ == "__main__":
    main()
