"""METAR and TAF from the NOAA/NWS Aviation Weather Center Data API, the primary weather source.

Docs: https://aviationweather.gov/data/api/  (free, no key, global ICAO coverage)

Limits from the docs: at most 100 requests a minute, at most 400 results a request, and
`date` reaches back 30 days only. Older METARs come from the IEM archive (sources/iem.py),
which also stands in for the hourly fetch when AWC fails. There is no free archive of
non-US TAFs, so TAFs go back 30 days at most.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import duckdb

from aviation import warehouse
from aviation.http import DEFAULT_TIMEOUT, session
from aviation.sources import iem

log = logging.getLogger(__name__)

BASE = "https://aviationweather.gov/api/data"
HISTORY_DAYS = 30
# Stay under 100 requests a minute.
REQUEST_PAUSE_S = 0.7


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_metar(icaos: list[str], hours: float = 3, end: datetime | None = None) -> list[dict]:
    """Every METAR/SPECI in the `hours` before `end` (default now). Overlapping windows
    are harmless because rows are keyed on (icao, obsTime)."""
    params = {"ids": ",".join(icaos), "format": "json", "hours": hours}
    if end is not None:
        params["date"] = _iso(end)
    r = session().get(f"{BASE}/metar", params=params, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json() if r.content else []


def fetch_taf(icaos: list[str], at: datetime | None = None) -> list[dict]:
    """The TAFs current now, or those that were current at `at` (last 30 days only)."""
    params = {"ids": ",".join(icaos), "format": "json"}
    if at is not None:
        params["date"] = _iso(at)
    r = session().get(f"{BASE}/taf", params=params, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json() if r.content else []


def metar_rows(payload: list[dict]) -> list[dict]:
    now = warehouse.utcnow()
    return [
        {"icao": m["icaoId"], "obs_time": int(m["obsTime"]), "fetched_at": now, "payload": m}
        for m in payload
        if m.get("icaoId") and m.get("obsTime") is not None
    ]


def taf_rows(payload: list[dict]) -> list[dict]:
    now = warehouse.utcnow()
    return [
        {"icao": t["icaoId"], "issue_time": t["issueTime"], "fetched_at": now, "payload": t}
        for t in payload
        if t.get("icaoId") and t.get("issueTime")
    ]


def ingest_metar(con: duckdb.DuckDBPyConnection, icaos: list[str], hours: int = 3) -> int | str:
    try:
        rows = metar_rows(fetch_metar(icaos, hours))
    except Exception as exc:
        # AWC down or refusing: take the same window from IEM, which never overwrites.
        log.warning("AWC METAR failed (%s); falling back to IEM", exc)
        end = warehouse.utcnow()
        n = iem.ingest_metar(con, icaos, end - timedelta(hours=hours), end + timedelta(minutes=1))
        return f"{n} (IEM fallback; AWC failed: {exc})"
    n = warehouse.upsert(con, "raw.metar", rows, ["icao", "obs_time"])
    warehouse.log_run(con, "metar", n)
    return n


def ingest_taf(con: duckdb.DuckDBPyConnection, icaos: list[str]) -> int:
    n = warehouse.upsert(con, "raw.taf", taf_rows(fetch_taf(icaos)), ["icao", "issue_time"])
    warehouse.log_run(con, "taf", n)
    return n


def backfill_metar(con: duckdb.DuckDBPyConnection, icaos: list[str], days: int = HISTORY_DAYS,
                   chunk_hours: int = 6) -> int:
    """The last `days` (at most 30) of METARs from AWC, in chunks small enough to stay
    under its 400-result cap (7 airports at up to 4 reports an hour)."""
    end = warehouse.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = end - timedelta(days=min(days, HISTORY_DAYS)) + timedelta(hours=1)
    n = 0
    at = end
    while at > start:
        rows = metar_rows(fetch_metar(icaos, chunk_hours, at))
        if len(rows) >= 400:
            log.warning("AWC METAR chunk ending %s hit the 400-result cap", at)
        n += warehouse.upsert(con, "raw.metar", rows, ["icao", "obs_time"])
        at -= timedelta(hours=chunk_hours)
        time.sleep(REQUEST_PAUSE_S)
    warehouse.log_run(con, "metar_backfill", n, detail=f"awc {days} days")
    return n


def backfill_taf(con: duckdb.DuckDBPyConnection, icaos: list[str], days: int = HISTORY_DAYS,
                 step_hours: int = 1) -> int:
    """TAFs over the last `days` (at most 30). AWC returns only the TAF current at the
    requested time, so step through the window; an hourly step catches amendments."""
    now = warehouse.utcnow().replace(minute=0, second=0, microsecond=0)
    # Leave an hour's margin: a date right at the 30-day limit is refused.
    at = now - timedelta(days=min(days, HISTORY_DAYS)) + timedelta(hours=1)
    n = 0
    while at <= now:
        n += warehouse.upsert(con, "raw.taf", taf_rows(fetch_taf(icaos, at)), ["icao", "issue_time"])
        at += timedelta(hours=step_hours)
        time.sleep(REQUEST_PAUSE_S)
    warehouse.log_run(con, "taf_backfill", n, detail=f"awc {days} days")
    return n


def backfill(con: duckdb.DuckDBPyConnection, icaos: list[str], days: int) -> dict:
    """METARs for `days`: AWC for the last 30, IEM before that. TAFs for the last 30."""
    stats = {"metar_awc": backfill_metar(con, icaos, days), "taf_awc": backfill_taf(con, icaos, days)}
    if days > HISTORY_DAYS:
        end = warehouse.utcnow() - timedelta(days=HISTORY_DAYS) + timedelta(hours=1)
        start = (warehouse.utcnow() - timedelta(days=days)).replace(hour=0, minute=0, second=0,
                                                                   microsecond=0)
        stats["metar_iem"] = iem.ingest_metar(con, icaos, start, end)
    return stats
