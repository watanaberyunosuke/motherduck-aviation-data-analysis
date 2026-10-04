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
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

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
    now = now or datetime.now(timezone.utc)
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
    """
    in_list = ", ".join(f"'{c}'" for c in icaos)
    scope = f"and f.est_arrival_airport in ({in_list})" if arrivals_only else ""
    return con.execute(f"""
        select f.icao24, f.first_seen, f.last_seen
        from raw.opensky_flights f
        where f.last_seen >= ? and f.last_seen < ? {scope}
          and not exists (
              select 1 from raw.opensky_tracks t
              where t.icao24 = f.icao24
                and t.start_time between f.first_seen - 1800 and coalesce(f.last_seen, f.first_seen) )
        order by f.last_seen desc, f.icao24
        limit ?
    """, [begin, end, limit]).fetchall()


def ingest(con: duckdb.DuckDBPyConnection, icaos: list[str], cfg: dict) -> dict:
    client = OpenSkyClient(min_credits_remaining=int(cfg.get("min_credits_remaining", 200)))
    begin, end = day_window(int(cfg.get("days_back", 1)))
    stats = {"flights": 0, "tracks": 0, "stopped_early": ""}

    try:
        for icao in icaos:
            for direction in ("arrival", "departure"):
                rows = flight_rows(client.flights(direction, icao, begin, end))
                stats["flights"] += warehouse.upsert(con, "raw.opensky_flights", rows,
                                                     ["icao24", "first_seen"])

        todo = select_flights_to_track(con, icaos, begin, end,
                                       bool(cfg.get("track_arrivals_only", True)),
                                       int(cfg.get("max_tracks_per_run", 100)))
        for icao24, first_seen, last_seen in todo:
            # Any instant inside the flight identifies it; the midpoint is safest.
            midpoint = (first_seen + (last_seen or first_seen)) // 2
            t = client.track(icao24, midpoint)
            if t and t.get("path"):
                stats["tracks"] += warehouse.upsert(con, "raw.opensky_tracks", [track_row(t)],
                                                    ["icao24", "start_time"])
    except CreditsExhausted as exc:
        stats["stopped_early"] = str(exc)
        log.warning("OpenSky stopped early: %s", exc)
    except requests.HTTPError as exc:
        stats["stopped_early"] = f"HTTP error: {exc}"
        raise
    except Exception as exc:  # network errors, warehouse errors: still say what stopped it
        stats["stopped_early"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        warehouse.log_run(con, "opensky", stats["flights"] + stats["tracks"],
                          detail=f"{stats} credits={client.credits_remaining} "
                                 f"http={_status_summary(client.statuses)}")
    return stats


def _status_summary(statuses: dict[str, Counter]) -> dict[str, dict[int, int]]:
    return {endpoint: dict(sorted(c.items())) for endpoint, c in statuses.items()}
