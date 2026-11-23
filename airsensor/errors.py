"""The failure vocabulary: every failed read of the data source has exactly one ErrorKind.

Also holds the HTTP status classification shared by both sources (the JSON API client and
the browser scraper), so the same status always gives the same ErrorKind.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum


class ErrorKind(StrEnum):
    TIMEOUT = "timeout"                    # request, page load, map wait or feed wait took too long
    HTTP_5XX = "http_5xx"                  # the API (or the page, or its feed) answered with a server error
    HTTP_429 = "http_429"                  # rate limited; may carry Retry-After
    INVALID_RESPONSE = "invalid_response"  # not the JSON we expect, map missing, page error message, bot check
    STALE_DATA = "stale_data"              # the data has not been refreshed for STALE_AFTER_S
    NETWORK_ERROR = "network_error"        # DNS, refused, reset, offline (httpx transport error or net::ERR_*)
    BROWSER_ERROR = "browser_error"        # browser mode only: Chromium crashed, closed or failed to start


class SourceError(Exception):
    """Reading the data source failed. Raised by AlertsApi or Scraper; handled by Collector."""

    def __init__(
        self,
        kind: ErrorKind,
        reason: str,
        http_status: int | None = None,
        url: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason
        self.http_status = http_status
        self.url = url
        self.retry_after_s = retry_after_s


def error_for_status(status: int, url: str, headers: Mapping[str, str]) -> SourceError | None:
    """Classify an HTTP status. None means the response is fine."""
    if status < 400:
        return None
    if headers.get("cf-mitigated") == "challenge":
        return SourceError(ErrorKind.INVALID_RESPONSE, "bot_check", http_status=status, url=url)
    if status == 429:
        return SourceError(
            ErrorKind.HTTP_429, "rate limited", http_status=status, url=url,
            retry_after_s=parse_retry_after(headers.get("retry-after")),
        )
    if status >= 500:
        return SourceError(ErrorKind.HTTP_5XX, f"server error {status}", http_status=status, url=url)
    return SourceError(ErrorKind.INVALID_RESPONSE, f"unexpected HTTP status {status}", http_status=status, url=url)


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either a number of seconds or an HTTP date. Unparseable values are ignored."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
