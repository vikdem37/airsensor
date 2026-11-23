# AirSensor

TODO: rewrite readme
A small server that monitors air-raid alerts in **Lviv oblast, per raion**. It reads the JSON
backend of the public [alerts.in.ua](https://alerts.in.ua/) map directly, with one GET per poll
and no token. A headless-Chromium scraper of the map is kept as a backup. It keeps an alert history in
PostgreSQL and logs every failure in structured JSON: source errors, internet loss and service
events. Optionally, it sends a daily Telegram report.

## Quick start

```powershell
copy .env.example .env          # optional: every setting has a default
docker compose up --build -d
docker compose logs -f collector
```

- `http://localhost:8000/health`: service state. `/alerts`, `/stats` and `/outages` return data.
  If port 8000 is taken, set `HTTP_PORT` in `.env`; Compose uses it on both sides of the mapping.
- `./logs/events.jsonl` and `./logs/connectivity.jsonl`: the two JSON log files.
- `docker compose stop` stops the service gracefully.

## How it works

```
api.alerts.in.ua JSON ──► AlertsApi ─┐
  (or, SOURCE_MODE=browser:           ├─{raion: Alert(type, level)|None}──► Collector ──► Tracker ──► PostgreSQL ◄── HTTP API
   the map page ──► Scraper) ─────────┘          │ SourceError(kind)          │                          ▲
                                                 ▼                            ▼                          │
                                          (backoff, re-open      Connectivity: probes 8.8.8.8 / 1.1.1.1,
                                           the source)           state + outages ──────────────────────┤
                                                                                           └── Daily report ──► Telegram
logs: logs/events.jsonl, logs/connectivity.jsonl, stdout
```

| Module | Responsibility |
|---|---|
| [main.py](airsensor/main.py) | Startup order, a supervisor around every task, signal handling and graceful shutdown |
| [config.py](airsensor/config.py) | Reads and validates every environment variable; fixed facts (the 7 raions and their API uids, Kyiv time zone) |
| [logs.py](airsensor/logs.py) | JSON formatter, the two log files, secret masking |
| [alerts_api.py](airsensor/alerts_api.py) | Default source. One GET of `api.alerts.in.ua/v3/alerts/active.json`, decoded and validated. `fetch_alerts()` returns the whole country as a list; `read_state()` returns `{raion: Alert \| None}` or raises `SourceError` |
| [scraper.py](airsensor/scraper.py) | Backup source (`SOURCE_MODE=browser`). Owns Chromium and the open map page; same interface, no alert level |
| [errors.py](airsensor/errors.py) | The failure vocabulary: `ErrorKind`, `SourceError`, HTTP status classification |
| [collector.py](airsensor/collector.py) | The poll loop: read, apply, or retry with backoff |
| [connectivity.py](airsensor/connectivity.py) | Internet probes, connection state (`online` / `source_down` / `internet_down`), outages |
| [tracker.py](airsensor/tracker.py) | One state machine per raion: `CLEAR`, `ALERT(type)`, `UNKNOWN` |
| [db.py](airsensor/db.py) | PostgreSQL: connect with retry, schema, retried writes, read queries |
| [api.py](airsensor/api.py) | FastAPI endpoints (read-only) |
| [report.py](airsensor/report.py) | The daily Telegram report via aiogram |

### The JSON API (default source)

The map page loads its data from `api.alerts.in.ua/v3/alerts/active.json`. It needs **no
token, cookie or API key**, so there is no login and no token to refresh. The response says
"This API is private and may change anytime", so every response is validated and anything
unexpected fails loudly. Format (compact field names, decoded in
[alerts_api.py](airsensor/alerts_api.py)):

| field | meaning |
|---|---|
| `t` | location type: `o` oblast, `r` raion, `h` hromada, `c` city |
| `luid` | location uid: 27 = Lviv oblast; 88–94 = its raions (see `RAION_UIDS` in [config.py](airsensor/config.py)) |
| `n` / `ni` | name, given directly or as an index into `alerts.in.ua/assets/locations.json` |
| `at` | alert type: missing or 0 = `air-raid`, 1 = `artillery-shelling`, 2 = `urban-fights`, 6 = `info`. Other codes are stored as `at-<code>` with an `unknown_alert_type` warning |
| `al` | alert level: 1 = `yellow` (drones), missing = `red` (missiles / mass strike) |
| `s` / `u` / `f` | started / updated / finished, in seconds since 1640000000 |

Only raion and oblast records count. An oblast alert covers every raion that has none of its
own. Hromada and city records are out of scope. To see the decoded data as a JSON array:

```powershell
python -m airsensor.alerts_api          # the whole country
python -m airsensor.alerts_api --lviv   # {raion: {type, level} | null}
```

A level change during an alert (yellow ↔ red) updates `alerts.alert_level` and logs
`alert_level_changed`. It does not start a new alert.

### The browser scraper (backup)

With `SOURCE_MODE=browser` the old scraper reads the rendered map instead. The page stays
open between polls, and the scraper only **observes** the page's own feed requests. It cannot
see the alert level, so `alert_level` stays empty. The Docker image is still the Playwright
image so that this mode keeps working.

## Error handling

| Situation | What happens | Code |
|---|---|---|
| Any failed read | Classified into one `error_kind`: `timeout`, `http_5xx`, `http_429`, `invalid_response`, `stale_data`, `network_error` or `browser_error` (browser mode only) | [alerts_api.py](airsensor/alerts_api.py), [scraper.py](airsensor/scraper.py) |
| API response changed | Not JSON, no `alerts` list, no `meta.last_updated_at`, or a record without `i`/`luid`/`t` → `invalid_response`, never "no alerts". An HTML answer or a Cloudflare challenge → `invalid_response` with reason `bot_check`. A CDN copy older than `STALE_AFTER_S` (`Age` header) → `stale_data` | [alerts_api.py](airsensor/alerts_api.py) |
| Location names unavailable | `locations.json` is only used for readable names. If it fails, `locations_unavailable` is logged once and polling goes on | [alerts_api.py](airsensor/alerts_api.py) |
| Source or internet? | After every failure all probe targets are tried. If any target answers, the source is down; if none do, the internet is down | [connectivity.py](airsensor/connectivity.py) |
| Retry | Waits 1, 2, 4 … 60 s (at least the `Retry-After` value after a 429). Every attempt is a new request (browser mode: reloads the page); every 5th consecutive failure re-opens the source (a new HTTP client, or a new Chromium). Each attempt is logged with its number and the next delay | [collector.py](airsensor/collector.py) |
| Outage | The first failure opens one outage (a database row plus a log line). The first valid read closes it, logging the downtime and the number of attempts. If the cause changes mid-outage, the outage is closed and one of the new kind is opened | [connectivity.py](airsensor/connectivity.py) |
| Data stops arriving during an alert | Every raion becomes **UNKNOWN** (state unknown), never all-clear. On recovery: if the same alert is still active, its row stays open; if it is gone, it ends with `end_uncertain`; if it is new, it starts with `start_uncertain` | [tracker.py](airsensor/tracker.py) |
| Page structure changed (browser mode) | Missing or duplicated raions, a visible error message or a bot-check page fail loudly as `invalid_response`, never as "no alerts" | [scraper.py](airsensor/scraper.py) |
| Unknown alert type | Stored as it is, with an `unknown_alert_type` warning | [tracker.py](airsensor/tracker.py) |
| Service restart | Alerts left open are resolved like an outage recovery. The gap since they were last seen is stored as an outage of kind `service`. Outages that a crash left open are closed | [tracker.py](airsensor/tracker.py), [connectivity.py](airsensor/connectivity.py) |
| Database down | Connection errors are retried 3 times (1, 2, 4 s); other database errors are not retried. A write that still fails is logged as `db_write_failed` with the full record, so the log is the backup. At startup the service retries until the database is reachable. A wrong password or database name is reported as `config_error` with exit code 2 | [db.py](airsensor/db.py) |
| Bad configuration | Every variable is validated. Each problem is logged as `config_error` (field + reason, never a secret's value), then the service exits with code 2 | [config.py](airsensor/config.py) |
| Crash inside a task | The supervisor logs `unhandled_exception` with the traceback and restarts the task with backoff | [main.py](airsensor/main.py) |
| Telegram | Network errors, server errors and flood control are retried 3 times. Authorisation, bad-request and other API errors are logged without retrying | [report.py](airsensor/report.py) |
| HTTP API | Bad parameters return 400. Database errors return 503 and log `db_read_failed`. Anything else returns 500 and logs `unhandled_exception` | [api.py](airsensor/api.py) |

## Logs

There is one JSON object per line, with these common fields: `ts` (UTC, milliseconds), `level`,
`logger`, `event`, `msg`, the event's own fields, and `exc` (the traceback) where relevant.
`airsensor.events` writes to `logs/events.jsonl` and `airsensor.connectivity` to
`logs/connectivity.jsonl`. Both also write to stdout. The files rotate at midnight UTC and the
last 14 are kept. The bot token and the database password are masked in every line. Successful
polls are logged only at `DEBUG`.

```json
{"ts": "2026-11-18T21:05:12.418Z", "level": "WARNING", "logger": "airsensor.connectivity", "event": "source_request_failed", "msg": "Reading the alert source failed (http_5xx: server error 503); attempt 2, next try in 2s", "error_kind": "http_5xx", "http_status": 503, "url": "http://stub:8080/v3/alerts/active.json", "reason": "server error 503", "attempt": 2, "next_retry_s": 2.0}
```

| event | file | level | key fields |
|---|---|---|---|
| `service_started` | events | INFO | version, config (secrets masked) |
| `service_stopping` / `service_stopped` | events | INFO | signal, uptime_s, exit_code |
| `service_downtime_recorded` | events | WARNING | gap_s, outage_id, open_alerts |
| `config_error` | events | ERROR | field, reason |
| `unhandled_exception` | events | ERROR | task, restart_in_s, exc |
| `db_unavailable` | events | ERROR | attempt, error, next_retry_s |
| `db_write_retry` / `db_write_failed` | events | WARNING / ERROR | attempt, error, record |
| `db_read_failed` | events | ERROR | path, error |
| `alert_started` | events | INFO | raion, alert_type, alert_level, started_at, start_uncertain, alert_id |
| `alert_level_changed` | events | INFO | raion, alert_type, previous_level, alert_level, alert_id |
| `alert_ended` | events | INFO | raion, alert_type, duration_s, end_uncertain, alert_id |
| `alert_state_unknown` / `alert_state_restored` | events | WARNING / INFO | raions, open_alerts, outage_id |
| `unknown_alert_type` | events | WARNING | raion, value |
| `stale_alert_closed` | events | WARNING | alert_id, raion |
| `report_sent` / `report_failed` / `report_disabled` | events | INFO / WARNING or ERROR / INFO | date, attempt, error, will_retry |
| `log_file_unavailable` | events | ERROR | path, error |
| `source_client_started` / `source_client_restarted` | connectivity | INFO / WARNING | reason |
| `locations_unavailable` | connectivity | WARNING | error_kind, reason, url |
| `browser_started` / `browser_restarted` (browser mode) | connectivity | INFO / WARNING | reason |
| `page_reloaded_by_site` | connectivity | INFO | |
| `source_request_failed` | connectivity | WARNING | error_kind, http_status, url, reason, attempt, next_retry_s |
| `source_unavailable` | connectivity | ERROR | outage_id, error_kind, http_status, reason, previous_outage_id |
| `source_recovered` | connectivity | INFO | outage_id, downtime_s, attempts |
| `internet_probe_failed` | connectivity | WARNING (DEBUG if repeated) | target, error |
| `internet_lost` / `internet_restored` | connectivity | ERROR / INFO | outage_id, error_kind / downtime_s, attempts |
| `retry_early` | connectivity | INFO | |
| `outage_interrupted` | connectivity | WARNING | outage_id, kind, downtime_s, reason |
| `source_poll_ok`, `page_reload_scheduled`, `browser_close_failed`, `report_scheduled` | both | DEBUG | |

Warnings and errors from libraries (uvicorn, asyncpg, aiogram) go to stdout as `library_log`.

## HTTP API

Times in query parameters are ISO 8601 dates or datetimes; values without a time zone are read
as Kyiv time, and a date-only `to` includes that whole day. Responses show Kyiv time.

| Endpoint | Returns |
|---|---|
| `GET /health` | 200 if the last read was valid and the database answers, otherwise 503. The body has the source state, the time of the last success, the open outage, the internet state, the database state and the uptime |
| `GET /alerts?raion=&type=&from=&to=&limit=` | Alerts active at any moment in the window, newest first, with `alert_level`, `duration_s` and the uncertainty flags |
| `GET /stats?from=&to=` | Per raion: alert count, total duration, longest alert. Only the part inside the window counts |
| `GET /outages?from=&to=&limit=` | The connection-failure log: kind (`source`, `internet`, `service`), reason, downtime, attempts |

Interactive docs are at `http://localhost:8000/docs`.

[`insomnia_collection.json`](insomnia_collection.json) has a request for every endpoint, with the
filters and the error responses (Insomnia: Import → From File). It targets `http://localhost:8000`.
If you changed `HTTP_PORT`, edit `base_url` in its Base Environment. The `day` variable sets the date
used by the "on a day" requests.

## Daily report

Set `BOT_TOKEN` and `ADMIN_CHAT_ID` in `.env` to turn it on. At `REPORT_TIME` (08:00 Kyiv time by
default) the admin receives, for the previous calendar day in Kyiv time (00:00 to 24:00):
- alert count, total duration and longest alert per raion;
- the number of outages and the total downtime by kind.

Anything that happens today appears in tomorrow's report, even if `REPORT_TIME` is set to a few
minutes after a test. To see today's figures now, run the command below with today's date. It also
sends the report if the bot is configured.

The report text is in Ukrainian. To print or send a report by hand:

```powershell
docker compose exec collector python -m airsensor.report 2026-11-17
```

## Demonstrating the error handling

**Internet loss** (the database keeps working on its internal network):

```powershell
docker network disconnect airsensor_egress airsensor-collector-1
```

The connectivity log then shows `source_request_failed` → `internet_probe_failed` →
`internet_lost`, and the events log shows `alert_state_unknown`. Reconnect with:

```powershell
docker network connect airsensor_egress airsensor-collector-1
```

This logs `internet_restored` with `downtime_s` and `attempts`. The HTTP port goes through the
same network, so `/health` is unreachable while disconnected. Follow the logs instead.

**Source failures with the internet up.** Run the demo overlay. It points the collector at a
stub of the API (and of the map page, for browser mode) that can be told to fail
([stub/stub_server.py](stub/stub_server.py)):

```powershell
docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build -d
```

```powershell
curl.exe "localhost:8080/control?mode=http_503"
```

Other modes: `http_429`, `feed_503`, `feed_hang`, `slow`, `broken`, `error_page` and `bot_check`.
`mode=ok` returns to normal. Alerts can be started and ended on demand, for example
`curl.exe "localhost:8080/control?raion=stryi&type=air-raid"`, or `type=yellow:air-raid` for a
yellow-level alert (change it to `air-raid` to see `alert_level_changed`). Combine the two to see an alert
continue through an outage, or end with `end_uncertain`. To leave the demo, run
`docker compose up -d` without the overlay.

**Configuration error:**

```powershell
docker compose run --rm -e POLL_INTERVAL_S=abc collector
```

This logs `config_error` and exits with code 2.

**Restart during an alert.** With the demo overlay running, start an alert with the stub. Then
stop the collector, wait a minute, and start it again:

```powershell
docker compose -f docker-compose.yml -f docker-compose.demo.yml stop collector
```

```powershell
docker compose -f docker-compose.yml -f docker-compose.demo.yml start collector
```

The log shows `service_downtime_recorded`, and the gap appears in `/outages` as kind `service`.
If the alert is still active, its row stays open.

## Tests

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

The suite needs no network, browser or database (the API client is tested with
`httpx.MockTransport`; the scraper tests are skipped if Playwright is not installed). It covers
API decoding and validation and the error-handling rules: config validation,
error classification, backoff, outages and source-vs-internet detection, the UNKNOWN-state rules,
restart recovery, Telegram retries, API error responses and log formatting.

## Facts checked on the live API (2026-11-19)

- `GET api.alerts.in.ua/v3/alerts/active.json` answers 200 with plain JSON to a client with no
  cookies, no `Authorization` header and a plain `AirSensor/<version>` User-Agent. Cloudflare
  did not challenge it (from one home connection; this may change). The same data also exists
  as `active.mp` (MessagePack, used by the page) and `active.md` (a text report).
- Lviv raion uids were confirmed with `v3/regions/27/alerts/recent.json` (raion history for
  uid 27, Lviv oblast). The `al` field matches the red/yellow counts of the text report.
- `at` codes 0, 1, 2 were matched against `data-alert-type` on the rendered map; 6 (`info`) comes
  from the site's code. Codes 3, 4, 5, 90 and 99 exist, but their names are not confirmed.
- On the map page, some location names use Latin look-alike letters (e.g. «Вoвчaнськa»).
  The API uses uids, so this does not affect the default source.

## Facts checked on the live site (2026-11-18, browser mode)

- Headless Chromium loads https://alerts.in.ua/ without a Cloudflare challenge (from one home
  connection; this may change).
- The 7 Lviv raion paths have exactly the names in [config.py](airsensor/config.py), plus one
  oblast-level path without `data-raion`.
- The page fetches `api.alerts.in.ua/v3/alerts/active.mp` about every 11–16 s, so
  `STALE_AFTER_S=120` leaves a wide margin. In one test run the page did not refresh for 75 s.
  That case is covered: stale data leads to a page reload.
- **The map paths appear about 0.3 s before the feed data is applied to them.** Reading right after
  a load would show a false all-clear, so after every load the scraper waits for the first feed
  response plus 2 s.
- The heading «Не вдалося завантажити мапу тривог.» is always in the DOM, but hidden. It only
  counts when visible.
- Hromada-level paths (`data-hromada`) can appear with the same `data-raion`. They are ignored, so
  only raion-level alerts are tracked.
- Alert types seen: `air-raid`, `artillery-shelling`.

Still open (see CONTEXT.md §8):
- whether a whole-oblast alert lights the oblast path, the raion paths or both (both are handled);
- the full list of alert types.

## Known limitations

- The JSON API is private and undocumented. If it changes, the service fails loudly
  (`invalid_response`); `SOURCE_MODE=browser` is the fallback until the code is updated.
  `read_state()` is the only function to replace for another source (for example the official
  API at api.alerts.in.ua/v1, which needs a token by request; that token is static, with no refresh).
- Alert start and end times are the poll times, not the API's own `s`/`f` timestamps, so they
  are accurate to `POLL_INTERVAL_S`.
- Service downtime is only recorded when alerts were open at shutdown, because their
  `last_seen_at` is the only "last alive" timestamp.
- An outage left open by a crash (not a normal stop) is closed at the next startup. Its real end
  and its attempt count are unknown, and it can overlap the `service` outage recorded for the
  same gap.
- If both probe targets are blocked while the internet works, a source failure is reported as
  internet loss. `PROBE_TARGETS` can be changed.
- The collector runs as root inside its container, which is the Playwright image's default.
  If the browser fallback is dropped, the image can become `python:3.12-slim` (~150 MB instead of ~1.5 GB).
