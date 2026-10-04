"""Vercel function: hands the browser the warehouse tables the Airport conditions page needs.

The page (web/, built from the MotherDuck Dive) runs its SQL in the browser on
DuckDB-WASM. This function is the only part that talks to MotherDuck, so the token stays
server-side: it exports each allow-listed table as Parquet, and Vercel's edge (not the
browser) caches the file for 10 minutes. The pipeline lands new data hourly, so that is
fresh enough.

It also proxies live aircraft positions around each airport from OpenSky (/api/live),
because OpenSky does not allow browser requests from other sites. Each airport's answer
is cached at the edge for 2 minutes, so all viewers share one OpenSky call.

Self-contained on purpose: no import of the `aviation` package. Reads only, never writes.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Vercel's Python sandbox doesn't set $HOME, which duckdb needs (for its extension
# cache, the motherduck extension in particular) before it will attach an md: target.
os.environ.setdefault("HOME", "/tmp")

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response

app = FastAPI()

# Every table the Dive queries, with the rows it can need. Windows must cover the
# Dive's own date filters (30 days at most, except the all-time weather penalty on
# fct_arrival_weather_impact); they keep each file well under Vercel's 4.5 MB
# response limit as history grows.
TABLES = {
    "reference.airports": "true",
    "reference.airlines": "true",
    "marts.fct_airport_conditions": "true",
    "marts.fct_airport_weather_hourly": "hour_utc >= now() - interval 31 day",
    "marts.fct_daily_airport_movements": "day_utc >= current_date - 31",
    "marts.fct_arrivals": "arrived_at >= now() - interval 31 day",
    "marts.fct_departures": "departed_at >= now() - interval 31 day",
    "marts.fct_arrival_weather_impact": "true",
    "marts.fct_terminal_tracks": "point_at >= now() - interval 3 day",
}

# Vercel's edge caches each export for 10 minutes (stale-while-revalidate: the first
# request after that refreshes it in the background). Browsers must not keep their own
# stale copy, or a viewer would see old columns for up to an hour after a dbt change.
CACHE_HEADERS = {
    "Cache-Control": "no-cache",
    "Vercel-CDN-Cache-Control": "max-age=600, stale-while-revalidate=3600",
}

_con: duckdb.DuckDBPyConnection | None = None


def get_connection() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        _con = duckdb.connect(os.environ.get("WAREHOUSE", "md:aviation"))
    return _con


@app.get("/api/tables/{name}")
def table(name: str) -> Response:
    if name not in TABLES:
        raise HTTPException(404, f"unknown table {name!r}")
    # One cursor per request: FastAPI runs sync handlers on a thread pool, and a duckdb
    # connection must not be shared across threads.
    cur = get_connection().cursor()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "table.parquet"
        try:
            cur.execute(f"copy (select * from {name} where {TABLES[name]}) "
                        f"to '{path}' (format parquet, compression zstd)")
        except duckdb.Error as exc:
            print(f"export of {name} failed: {exc}")
            raise HTTPException(503, f"{name} is not available yet") from exc
        finally:
            cur.close()
        body = path.read_bytes()
    return Response(body, media_type="application/vnd.apache.parquet", headers=CACHE_HEADERS)


# --- Live positions -----------------------------------------------------------------

OPENSKY_STATES = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network"
                     "/protocol/openid-connect/token")
# A 5 x 5 degree box (~550 km) around the airport. OpenSky charges 1 credit for boxes up
# to 25 square degrees.
LIVE_BOX_DEG = 2.5
LIVE_HEADERS = {
    "Cache-Control": "no-cache",
    "Vercel-CDN-Cache-Control": "max-age=120",
    # The MotherDuck-hosted Dive calls this from another origin; the data is public.
    "Access-Control-Allow-Origin": "*",
}

_token: tuple[str, float] | None = None


def _opensky_auth() -> dict:
    """Bearer header when OPENSKY_CLIENT_ID/SECRET are set (4,000 credits a day), else
    anonymous (400 a day)."""
    global _token
    cid, secret = os.environ.get("OPENSKY_CLIENT_ID"), os.environ.get("OPENSKY_CLIENT_SECRET")
    if not (cid and secret):
        return {}
    if _token is None or time.time() > _token[1] - 60:
        body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                       "client_id": cid, "client_secret": secret}).encode()
        with urllib.request.urlopen(OPENSKY_TOKEN_URL, data=body, timeout=10) as r:
            tok = json.load(r)
        _token = (tok["access_token"], time.time() + int(tok.get("expires_in", 1800)))
    return {"Authorization": f"Bearer {_token[0]}"}


def _live_error(status: int, detail: str) -> JSONResponse:
    # Errors are not edge-cached, so the next viewer retries.
    return JSONResponse({"detail": detail}, status_code=status,
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})


@app.get("/api/live/{icao}")
def live(icao: str) -> JSONResponse:
    cur = get_connection().cursor()
    try:
        row = cur.execute("select lat, lon from reference.airports where icao = ?",
                          [icao]).fetchone()
    finally:
        cur.close()
    if row is None:
        return _live_error(404, f"unknown airport {icao!r}")
    lat, lon = row
    query = urllib.parse.urlencode({"lamin": lat - LIVE_BOX_DEG, "lamax": lat + LIVE_BOX_DEG,
                                    "lomin": lon - LIVE_BOX_DEG, "lomax": lon + LIVE_BOX_DEG})
    try:
        req = urllib.request.Request(f"{OPENSKY_STATES}?{query}", headers=_opensky_auth())
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return _live_error(503, "OpenSky's daily quota for live positions is used up")
        return _live_error(502, f"OpenSky returned HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError) as exc:
        return _live_error(502, f"OpenSky unreachable: {exc}")

    # State vector fields: https://openskynetwork.github.io/opensky-api/rest.html
    aircraft = [{
        "icao24": s[0],
        "callsign": (s[1] or "").strip() or None,
        "lon": s[5],
        "lat": s[6],
        "alt_ft": round((s[7] if s[7] is not None else s[13] or 0) * 3.28084),
        "on_ground": bool(s[8]),
        "speed_kt": None if s[9] is None else round(s[9] * 1.94384),
        "track_deg": s[10],
        "vrate_fpm": None if s[11] is None else round(s[11] * 196.85),
    } for s in payload.get("states") or [] if s[5] is not None and s[6] is not None]
    return JSONResponse({"time": payload.get("time"), "aircraft": aircraft}, headers=LIVE_HEADERS)
