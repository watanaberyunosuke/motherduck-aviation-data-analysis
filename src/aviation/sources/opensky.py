"""ADS-B flights and flight paths from the OpenSky Network REST API.

Docs: https://openskynetwork.github.io/opensky-api/rest.html

Constraints that shape this module (all from OpenSky's docs):
  * Flights and tracks need an authenticated account. New accounts use OAuth2 client
    credentials; basic auth only works for accounts created before March 2025.
  * /flights/* is batch-processed overnight, so only completed UTC days are pulled.
  * /tracks/all is labelled experimental and only serves the last 30 days.
  * Each endpoint has its own daily credit quota; the balance is returned in the
    X-Rate-Limit-Remaining header. This module stops early rather than hitting 429.
    A /tracks/all call costs several credits (4 to 30 seen in Oct 2026), so the tracks
    quota covers far fewer flights than there are calls in the credit balance.

What OpenSky does NOT provide: scheduled departure/arrival times. It observes aircraft,
it does not know timetables, so schedule-based "delay" cannot be computed from it. The
dbt marts measure observable performance instead (see README, section 3).

OpenSky's receiver coverage is densest in Europe and North America. Expect gaps for
Australian and Asian airports, particularly at low altitude on approach.

OpenSky is the preferred flights source, and the only source of flight paths.
AeroDataBox (sources/aerodatabox.py) is the fallback: it fills the days OpenSky returned
nothing for, or everything OpenSky has not loaded when OpenSky fails. Each airport-day
costs 30 of the roughly 4,000 daily flights credits, so filling the `backfill_days` window
(30 days) takes a few days of hourly runs; after that each day's run loads just the new day.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import duckdb
import requests

from aviation import warehouse
from aviation.config import require_env
from aviation.http import DEFAULT_TIMEOUT, session

log = logging.getLogger(__name__)

API = "https://opensky-network.org/api"
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"


class CreditsExhausted(RuntimeError):
    pass


class OpenSkyClient:
    def __init__(self, min_credits_remaining: int = 200):
        self._s = session()
        self._token: str | None = None
        self._token_expires = 0.0
        self.min_credits_remaining = min_credits_remaining
        self.credits_remaining: dict[str, int] = {}
        # HTTP status codes per endpoint, e.g. {"tracks": Counter({200: 12, 404: 3})}.
        # Logged with each run, so a run that stores nothing shows why.
        self.statuses: dict[str, Counter] = {}

    def _auth_header(self) -> dict:
        if not self._token or time.time() > self._token_expires - 60:
            r = self._s.post(TOKEN_URL, data={
                "grant_type": "client_credentials",
                "client_id": require_env("OPENSKY_CLIENT_ID"),
                "client_secret": require_env("OPENSKY_CLIENT_SECRET"),
            }, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_expires = time.time() + int(body.get("expires_in", 1800))
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, endpoint: str, path: str, params: dict):
        remaining = self.credits_remaining.get(endpoint)
        if remaining is not None and remaining < self.min_credits_remaining:
            raise CreditsExhausted(f"{endpoint}: {remaining} credits left, below safety floor")

        r = self._s.get(f"{API}{path}", params=params, headers=self._auth_header(),
                        timeout=DEFAULT_TIMEOUT)
        self.statuses.setdefault(endpoint, Counter())[r.status_code] += 1
        if "X-Rate-Limit-Remaining" in r.headers:
            self.credits_remaining[endpoint] = int(r.headers["X-Rate-Limit-Remaining"])
        if r.status_code == 429:
            wait = r.headers.get("X-Rate-Limit-Retry-After-Seconds", "?")
            raise CreditsExhausted(f"{endpoint}: 429, retry after {wait}s")
        if r.status_code == 404:  # OpenSky returns 404 when there is simply no data
            return None
        r.raise_for_status()
        return r.json()

    def flights(self, direction: str, airport: str, begin: int, end: int) -> list[dict]:
        return self._get("flights", f"/flights/{direction}",
                         {"airport": airport, "begin": begin, "end": end}) or []

    def track(self, icao24: str, at: int) -> dict | None:
        return self._get("tracks", "/tracks/all", {"icao24": icao24, "time": at})


def day_window(days_back: int, now: datetime | None = None) -> tuple[int, int]:
    """[00:00, 24:00) UTC of the day `days_back` days ago."""
    now = now or warehouse.utcnow()
    day = (now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day.timestamp()), int((day + timedelta(days=1)).timestamp())


def flight_rows(flights: list[dict]) -> list[dict]:
    now = warehouse.utcnow()
    return [{
        "icao24": f["icao24"],
        "first_seen": int(f["firstSeen"]),
        "last_seen": f.get("lastSeen"),
        "callsign": (f.get("callsign") or "").strip() or None,
        "est_departure_airport": f.get("estDepartureAirport"),
        "est_arrival_airport": f.get("estArrivalAirport"),
        "fetched_at": now,
        "payload": f,
    } for f in flights if f.get("icao24") and f.get("firstSeen") is not None]


def track_row(t: dict) -> dict:
    return {
        "icao24": t["icao24"],
        "start_time": int(t["startTime"]),
        "end_time": t.get("endTime"),
        "callsign": (t.get("callsign") or "").strip() or None,
        "fetched_at": warehouse.utcnow(),
        "payload": t,
    }


def select_flights_to_track(con: duckdb.DuckDBPyConnection, icaos: list[str],
                            begin: int, end: int, arrivals_only: bool, limit: int) -> list[tuple]:
    """Flights in the window that have no stored track yet, most recently landed first.

    Newest first because the tracks quota runs out long before the list does, and the
    oldest flights are the first to age out of /tracks. `arrivals_only` keeps flights
    landing at an in-scope airport, from any origin; departures to elsewhere are skipped.
    Flights come from both sources: AeroDataBox flights with a Mode-S address can be
    tracked as well as OpenSky's own.
    """
    in_list = ", ".join(f"'{c}'" for c in icaos)
    os_scope = f"and est_arrival_airport in ({in_list})" if arrivals_only else ""
    adb_scope = "and direction = 'arrival'" if arrivals_only else ""
    return con.execute(f"""
        with flights as (
            select icao24, first_seen, last_seen
            from raw.opensky_flights
            where last_seen >= $begin and last_seen < $end {os_scope}
            union all
            select icao24, departure_epoch, arrival_epoch
            from raw.aerodatabox_flights
            where icao24 is not null and departure_epoch is not null
              and arrival_epoch >= $begin and arrival_epoch < $end
              and coalesce(status, '') not in ('Canceled', 'CanceledUncertain', 'Diverted')
              {adb_scope}
        )
        select icao24, min(first_seen), max(last_seen)
        from flights f
        where not exists (
            select 1 from raw.opensky_tracks t
            where t.icao24 = f.icao24
              and t.start_time between f.first_seen - 1800 and coalesce(f.last_seen, f.first_seen))
        -- The same flight from both sources: one call is enough.
        group by icao24, last_seen // 3600
        order by max(last_seen) desc, icao24
        limit $limit
    """, {"begin": begin, "end": end, "limit": limit}).fetchall()


def done_slots(con: duckdb.DuckDBPyConnection) -> set[tuple[str, str, date]]:
    """(icao, direction, day) slots OpenSky has already fetched. Slots AeroDataBox loaded
    are fetched again: OpenSky is preferred, and staging switches to it once it has them."""
    return {tuple(r) for r in con.execute(
        "select icao, direction, day_utc from raw.flight_slots where source = 'opensky'"
    ).fetchall()}


def ingest_slot(con: duckdb.DuckDBPyConnection, client: OpenSkyClient, icao: str,
                direction: str, begin: int) -> int:
    rows = flight_rows(client.flights(direction, icao, begin, begin + 86400))
    n = warehouse.upsert(con, "raw.opensky_flights", rows, ["icao24", "first_seen"])
    warehouse.mark_slot(con, "opensky", icao, direction,
                        datetime.fromtimestamp(begin, timezone.utc).date(), n)
    return n


def ingest(con: duckdb.DuckDBPyConnection, icaos: list[str], cfg: dict) -> dict:
    """Every (airport, direction) day OpenSky has not loaded, from the newest complete day
    back to `backfill_days`, newest first, then tracks for the newest day.

    Loaded slots are skipped, so the hourly run spends credits only on gaps: a new day
    once a day, older days until the window is full, and more tracks while the tracks
    quota lasts. Flights and tracks have separate quotas, so one running out does not
    stop the other.
    """
    client = OpenSkyClient(min_credits_remaining=int(cfg.get("min_credits_remaining", 200)))
    newest = warehouse.newest_complete_day(int(cfg.get("days_back", 1)))
    oldest = max(newest, int(cfg.get("backfill_days", newest)))
    # No cap unless configured: the credit floor is what stops a run.
    budget = int(cfg.get("max_backfill_calls_per_run") or 10**6)
    stats = {"flights": 0, "tracks": 0, "slots": 0, "stopped_early": ""}

    try:
        done = done_slots(con)
        try:
            for back in range(newest, oldest + 1):
                day_begin, _ = day_window(back)
                day = datetime.fromtimestamp(day_begin, timezone.utc).date()
                for icao in icaos:
                    for direction in ("arrival", "departure"):
                        if (icao, direction, day) in done:
                            continue
                        if stats["slots"] >= budget:
                            raise CreditsExhausted(f"budget of {budget} calls spent")
                        stats["flights"] += ingest_slot(con, client, icao, direction, day_begin)
                        stats["slots"] += 1
        except CreditsExhausted as exc:
            stats["stopped_early"] = f"flights: {exc}; "
            log.warning("OpenSky flights stopped: %s", exc)

        begin, end = day_window(newest)
        todo = select_flights_to_track(con, icaos, begin, end,
                                       bool(cfg.get("track_arrivals_only", True)),
                                       int(cfg.get("max_tracks_per_run", 100)))
        try:
            for icao24, first_seen, last_seen in todo:
                # Any instant inside the flight identifies it; the midpoint is safest.
                midpoint = (first_seen + (last_seen or first_seen)) // 2
                t = client.track(icao24, midpoint)
                if t and t.get("path"):
                    stats["tracks"] += warehouse.upsert(con, "raw.opensky_tracks", [track_row(t)],
                                                        ["icao24", "start_time"])
        except CreditsExhausted as exc:
            stats["stopped_early"] += f"tracks: {exc}"
            log.warning("OpenSky tracks stopped: %s", exc)
    except requests.HTTPError as exc:
        stats["stopped_early"] += f"HTTP error: {exc}"
        raise
    except Exception as exc:  # network errors, warehouse errors: still say what stopped it
        stats["stopped_early"] += f"{type(exc).__name__}: {exc}"
        raise
    finally:
        warehouse.log_run(con, "opensky", stats["flights"] + stats["tracks"],
                          detail=f"{stats} credits={client.credits_remaining} "
                                 f"http={_status_summary(client.statuses)}")
    return stats


def _status_summary(statuses: dict[str, Counter]) -> dict[str, dict[int, int]]:
    return {endpoint: dict(sorted(c.items())) for endpoint, c in statuses.items()}
