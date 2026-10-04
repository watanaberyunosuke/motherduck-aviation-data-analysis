"""Airport departures and arrivals from AeroDataBox, the preferred flights source.

Docs: https://doc.aerodatabox.com  (paid; sold through RapidAPI, API.market or directly)

Unlike OpenSky, AeroDataBox knows the timetable: each flight carries its scheduled time
next to the revised (actual or estimated) and runway times, so schedule delay can be
measured instead of inferred. OpenSky stays as the fallback: dbt prefers AeroDataBox for
every (airport, direction, UTC day) slot it has loaded and uses OpenSky for the rest, and
OpenSky remains the only source of flight paths.

Endpoint: GET /flights/airports/icao/{icao}/{fromLocal}/{toLocal} ("FIDS", TIER 2). One
call returns both directions for at most 12 hours of local time on most plans, so a UTC
day costs two calls per airport. withLeg=true adds the other end of each flight (origin
for arrivals, destination for departures). How far back it reaches depends on the plan
(about a year at most); `backfill_days` in config/airports.yml (30) is well inside it.

Credentials: AERODATABOX_KEY, the key of whichever marketplace `provider` names. Without
it the source is skipped and every slot falls back to OpenSky.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb

from aviation import warehouse
from aviation.http import DEFAULT_TIMEOUT, session

log = logging.getLogger(__name__)

CREDENTIALS = ("AERODATABOX_KEY",)
PROVIDERS = {
    # provider: (base URL, headers given the key)
    "rapidapi": ("https://aerodatabox.p.rapidapi.com",
                 lambda key: {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"}),
    "apimarket": ("https://prod.api.market/api/v1/aedbx/aerodatabox",
                  lambda key: {"x-api-market-key": key}),
}
MAX_WINDOW = timedelta(hours=12)
CANCELLED = ("Canceled", "CanceledUncertain", "Diverted")


class QuotaExhausted(RuntimeError):
    pass


class AeroDataBoxClient:
    def __init__(self, provider: str = "rapidapi", key: str | None = None,
                 pause_s: float = 0.6):
        if provider not in PROVIDERS:
            raise ValueError(f"aerodatabox provider must be one of {sorted(PROVIDERS)}, not {provider!r}")
        self.base, headers = PROVIDERS[provider]
        self._s = session()
        self._s.headers.update(headers(key or os.environ["AERODATABOX_KEY"].strip()))
        self.pause_s = pause_s
        self.calls = 0

    def fids(self, icao: str, start_local: datetime, end_local: datetime) -> dict:
        fmt = "%Y-%m-%dT%H:%M"
        url = f"{self.base}/flights/airports/icao/{icao}/{start_local:{fmt}}/{end_local:{fmt}}"
        if self.calls:
            time.sleep(self.pause_s)  # plans allow 1-2 requests a second
        r = self._s.get(url, params={
            "withLeg": "true", "direction": "Both", "withCancelled": "true",
            # A codeshare is the same aircraft under another airline's number; keep the
            # operating flight only, as OpenSky would see it.
            "withCodeshared": "false", "withCargo": "true", "withPrivate": "true",
        }, timeout=DEFAULT_TIMEOUT)
        self.calls += 1
        if r.status_code == 429:
            raise QuotaExhausted(f"429 from AeroDataBox: {r.text[:200]}")
        if r.status_code == 204 or not r.content:
            return {}
        r.raise_for_status()
        return r.json()


def windows(day: date, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """Local-time windows of at most 12 hours that together cover the UTC day.

    A UTC half-day is usually 12 local hours, but 13 across a clock change, so such a
    window is split. Ends are a minute short so consecutive windows do not overlap.
    """
    out = []
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    for half in range(2):
        a = start + half * MAX_WINDOW
        b = a + MAX_WINDOW
        la, lb = a.astimezone(tz).replace(tzinfo=None), b.astimezone(tz).replace(tzinfo=None)
        if lb - la > MAX_WINDOW:
            mid = (a + MAX_WINDOW / 2).astimezone(tz).replace(tzinfo=None)
            out += [(la, mid - timedelta(minutes=1)), (mid, lb - timedelta(minutes=1))]
        else:
            out.append((la, lb - timedelta(minutes=1)))
    return out


def _utc(movement: dict | None, field: str) -> datetime | None:
    value = ((movement or {}).get(field) or {}).get("utc")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def best_time(movement: dict | None) -> datetime | None:
    """Runway time, else revised (actual or estimated), else scheduled."""
    for field in ("runwayTime", "revisedTime", "scheduledTime"):
        if at := _utc(movement, field):
            return at
    return None


def flight_rows(icao: str, payload: dict) -> list[dict]:
    """One row per flight on the board. The board's own airport is omitted from the
    payload, so it is written back in for the staging model."""
    now = warehouse.utcnow()
    rows = []
    for direction, key in (("arrival", "arrivals"), ("departure", "departures")):
        for f in payload.get(key) or []:
            dep, arr = f.get("departure") or {}, f.get("arrival") or {}
            if f.get("movement"):
                # Without the leg: this airport's times, but the *other* airport's name.
                here = {k: v for k, v in f["movement"].items() if k != "airport"}
                there = {"airport": f["movement"].get("airport") or {}}
                dep, arr = (here, there) if direction == "departure" else (there, here)
            here = dep if direction == "departure" else arr
            here.setdefault("airport", {}).setdefault("icao", icao)
            f["departure"], f["arrival"] = dep, arr
            f.pop("movement", None)

            ident = ((f.get("number") or "").replace(" ", "").upper()
                     or (f.get("callSign") or "").strip().upper()
                     or ((f.get("aircraft") or {}).get("reg") or "").upper())
            sched = _utc(dep, "scheduledTime") or _utc(arr, "scheduledTime")
            if not ident or not sched:
                continue
            dep_at, arr_at = best_time(dep), best_time(arr)
            rows.append({
                "airport_icao": icao,
                "direction": direction,
                "flight_id": f"{ident}@{sched:%Y-%m-%dT%H:%MZ}",
                "icao24": ((f.get("aircraft") or {}).get("modeS") or "").lower() or None,
                "callsign": (f.get("callSign") or "").strip() or None,
                "departure_epoch": int(dep_at.timestamp()) if dep_at else None,
                "arrival_epoch": int(arr_at.timestamp()) if arr_at else None,
                "status": f.get("status"),
                "fetched_at": now,
                "payload": f,
            })
    return rows


def ingest_day(con: duckdb.DuckDBPyConnection, client: AeroDataBoxClient, icao: str,
               tz: ZoneInfo, day: date) -> int:
    """Both directions of one airport for one UTC day. The slot is only marked done once
    every window has loaded, so a failure part-way is retried next time."""
    rows = []
    for start, end in windows(day, tz):
        rows += flight_rows(icao, client.fids(icao, start, end))
    n = warehouse.upsert(con, "raw.aerodatabox_flights", rows,
                         ["airport_icao", "direction", "flight_id"])
    for direction in ("arrival", "departure"):
        warehouse.mark_slot(con, "aerodatabox", icao, direction, day,
                            sum(r["direction"] == direction for r in rows))
    return n


def done_days(con: duckdb.DuckDBPyConnection, icao: str) -> set[date]:
    return {d for (d,) in con.execute("""
        select day_utc from raw.flight_slots
        where source = 'aerodatabox' and icao = ?
        group by day_utc having count(distinct direction) = 2
    """, [icao]).fetchall()}


def ingest(con: duckdb.DuckDBPyConnection, timezones: dict[str, str], cfg: dict) -> dict:
    """Every airport-day not yet loaded, from the newest complete UTC day back to
    `backfill_days`, newest first, within `max_backfill_calls_per_run` calls. Loaded days
    are skipped, so the hourly run spends paid units only on gaps (a new day is 14 calls)."""
    client = AeroDataBoxClient(cfg.get("provider", "rapidapi"))
    today = warehouse.utcnow().date()
    newest = warehouse.newest_complete_day(int(cfg.get("days_back", 1)))
    oldest = max(newest, int(cfg.get("backfill_days", newest)))
    # Units are paid: without a configured cap, one new day's calls.
    budget = int(cfg.get("max_backfill_calls_per_run") or 2 * len(timezones))
    stats = {"flights": 0, "days": 0, "stopped_early": ""}
    try:
        # Round-robin by day, so every airport advances together.
        done = {icao: done_days(con, icao) for icao in timezones}
        for back in range(newest, oldest + 1):
            day = today - timedelta(days=back)
            for icao, tz in timezones.items():
                if day in done[icao]:
                    continue
                if client.calls + len(windows(day, ZoneInfo(tz))) > budget:
                    raise QuotaExhausted(f"budget of {budget} calls spent")
                stats["flights"] += ingest_day(con, client, icao, ZoneInfo(tz), day)
                stats["days"] += 1
    except QuotaExhausted as exc:
        stats["stopped_early"] = str(exc)
        log.info("AeroDataBox stopped: %s", exc)
    except Exception as exc:
        stats["stopped_early"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        warehouse.log_run(con, "aerodatabox", stats["flights"],
                          detail=f"{stats} calls={client.calls}")
    return stats
