"""Vercel function: hands the browser the warehouse tables the Airport conditions page needs.

The page (web/, built from the MotherDuck Dive) runs its SQL in the browser on
DuckDB-WASM. This function is the only part that talks to MotherDuck, so the token stays
server-side: it exports each allow-listed table as Parquet, and Vercel's edge (not the
browser) caches the file for 10 minutes. The pipeline lands new data hourly, so that is
fresh enough.

It also proxies live aircraft positions around each airport (/api/live), because the
ADS-B sources do not allow browser requests from other sites. OpenSky and adsb.lol are
asked together: OpenSky's aircraft first, then adsb.lol's that OpenSky lacks; either
answers alone if the other fails (OpenSky has timed out from Vercel's servers). Each
airport's answer is cached at the edge for 2 minutes, so all viewers share one call.

/api/snapshot/<icao> answers clients without DuckDB (the GroundKit apps) with one airport's
conditions, NOTAMs, recent weather and callsign history as JSON, from the same SQL.

Self-contained on purpose: no import of the `aviation` package. Reads only, never writes.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

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
    "reference.cargo_operators": "true",
    "marts.fct_airport_conditions": "true",
    "marts.fct_airport_weather_hourly": "hour_utc >= now() - interval 31 day",
    "marts.fct_daily_airport_movements": "day_utc >= current_date - 31",
    "marts.fct_arrivals": "arrived_at >= now() - interval 31 day",
    "marts.fct_departures": "departed_at >= now() - interval 31 day",
    "marts.fct_arrival_weather_impact": "true",
    "marts.fct_terminal_tracks": "point_at >= now() - interval 3 day",
    "marts.fct_notams": "is_current",
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


# --- Airport snapshot (JSON) ------------------------------------------------------------
# One airport's current state in one small JSON document, for clients without DuckDB
# (the GroundKit apps). The SQL mirrors the Dive's queries, so both read the marts the same
# way; the client combines it with /api/live for its arrival and departure boards.

def _json_value(v):
    if isinstance(v, datetime):
        # TIMESTAMPTZ comes back aware; anything naive is UTC (dbt forces TimeZone UTC).
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _rows(cur: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    cur.execute(sql, params or [])
    names = [d[0] for d in cur.description]
    return [{k: _json_value(v) for k, v in zip(names, row)} for row in cur.fetchall()]


SNAPSHOT_SQL = {
    "airports": """
        select icao, iata, name, timezone, lat, lon, notam_source
        from reference.airports
        order by iata
    """,
    "conditions": """
        select *, date_diff('minute', metar_observed_at, now()) as metar_age_min
        from marts.fct_airport_conditions
        where icao = ?
    """,
    # 24 hours is enough for a shift's trend (wind, gusts, temperature).
    "weather": """
        select hour_utc, observed_at, flight_category, wind_dir_deg, wind_variable,
               wind_speed_kt, wind_gust_kt, visibility_sm, ceiling_ft, wx_string,
               has_thunderstorm, has_precipitation, temp_c, dewpoint_c
        from marts.fct_airport_weather_hourly
        where icao = ? and hour_utc >= now() - interval 24 hour
        order by hour_utc
    """,
    # The NOTAMs fct_airport_conditions.notams_in_force counts.
    "notams": """
        select notam_key, number, q_code, category, condition, starts_at, ends_at,
               is_permanent, is_estimated, schedule, body, raw_text, is_runway_closure
        from marts.fct_notams
        where location = ? and is_current
        order by starts_at desc, number
    """,
    # Median minutes inside the 50 NM terminal area, for ETAs from live positions.
    "medians": """
        select
          (select median(terminal_minutes) from marts.fct_arrival_weather_impact
           where arrival_icao = $1 and arrived_at >= now() - interval 30 day) as arrival_terminal_minutes,
          (select median(departure_terminal_minutes) from marts.fct_departures
           where departure_icao = $1 and departed_at >= now() - interval 30 day) as departure_terminal_minutes
    """,
}

# Callsigns seen arriving at / departing from the airport in the last 30 days, with the
# usual origin / destination and usual local time of day (minutes after midnight, the
# median wrapped around midnight), as the Dive's historyQ. Flight numbers repeat daily,
# so the client predicts its boards from these. {tz} comes from reference.airports.
HISTORY_SQL = """
    with seen as (
      select callsign, 'inbound' as dir, coalesce(departure_iata, departure_icao) as other,
             arrived_at as seen_at, is_freighter
      from marts.fct_arrivals
      where arrival_icao = $1 and arrived_at >= now() - interval 30 day and callsign is not null
      union all
      select callsign, 'outbound', coalesce(arrival_iata, arrival_icao), departed_at, is_freighter
      from marts.fct_departures
      where departure_icao = $1 and departed_at >= now() - interval 30 day and callsign is not null
    ),
    timed as (
      select *,
        hour(seen_at at time zone '{tz}') * 60 + minute(seen_at at time zone '{tz}') as m,
        arg_min(hour(seen_at at time zone '{tz}') * 60 + minute(seen_at at time zone '{tz}'), seen_at)
          over (partition by callsign, dir) as ref
      from seen
    ),
    history as (
      select
        callsign, dir, mode(other) as other, count(*) as n, bool_or(is_freighter) as is_freighter,
        (((any_value(ref) + median(((((m - ref + 720) % 1440) + 1440) % 1440) - 720)) % 1440) + 1440) % 1440
          as usual_min,
        count(distinct cast(seen_at at time zone '{tz}' as date))
          filter (where seen_at >= now() - interval 14 day) as days_14
      from timed
      group by callsign, dir
    )
    -- QFA627 -> QF627, as stg_opensky_flights does.
    select h.*,
           al.iata || ltrim(regexp_extract(h.callsign, '^[A-Z]{{3}}(\\d{{1,4}})$', 1), '0')
             as flight_number_iata,
           al.name as airline_name
    from history h
    left join reference.airlines al
      on al.icao = regexp_extract(h.callsign, '^([A-Z]{{3}})\\d{{1,4}}$', 1)
     and al.iata is not null
    order by h.dir, h.usual_min
"""


@app.get("/api/snapshot/{icao}")
def snapshot(icao: str) -> JSONResponse:
    cur = get_connection().cursor()
    try:
        airports = _rows(cur, SNAPSHOT_SQL["airports"])
        airport = next((a for a in airports if a["icao"] == icao.upper()), None)
        if airport is None:
            raise HTTPException(404, f"unknown airport {icao!r}")
        icao = airport["icao"]
        conditions = _rows(cur, SNAPSHOT_SQL["conditions"], [icao])
        body = {
            "generated_at": _json_value(datetime.now(timezone.utc)),
            "airports": airports,
            "conditions": conditions[0] if conditions else None,
            "weather": _rows(cur, SNAPSHOT_SQL["weather"], [icao]),
            "notams": _rows(cur, SNAPSHOT_SQL["notams"], [icao]),
            "medians": _rows(cur, SNAPSHOT_SQL["medians"], [icao])[0],
            "history": _rows(cur, HISTORY_SQL.format(tz=airport["timezone"]), [icao]),
        }
    except duckdb.Error as exc:
        print(f"snapshot of {icao} failed: {exc}")
        raise HTTPException(503, f"{icao} is not available yet") from exc
    finally:
        cur.close()
    return JSONResponse(body, headers=CACHE_HEADERS)


# Observed arrival and departure paths of tracked flights over the last 3 days, as the
# Dive's map draws them (they trace the procedures in use). One compact line per track.
TRACKS_SQL = """
    select role,
           any_value(coalesce(flight_number_iata, callsign, icao24)) as label,
           any_value(departure_iata) as departure_iata,
           any_value(arrival_iata) as arrival_iata,
           list([round(lat, 4), round(lon, 4)] order by point_at) as points
    from marts.fct_terminal_tracks
    where airport_icao = ? and point_at >= now() - interval 3 day
    group by role, icao24, track_start_epoch
    order by role, min(point_at)
"""


@app.get("/api/tracks/{icao}")
def tracks(icao: str) -> JSONResponse:
    cur = get_connection().cursor()
    try:
        rows = _rows(cur, TRACKS_SQL, [icao.upper()])
    except duckdb.Error as exc:
        print(f"tracks of {icao} failed: {exc}")
        raise HTTPException(503, f"{icao} is not available yet") from exc
    finally:
        cur.close()
    return JSONResponse({"tracks": rows}, headers=CACHE_HEADERS)


# --- Live positions -----------------------------------------------------------------

OPENSKY_STATES = "https://opensky-network.org/api/states/all"
UA = {"User-Agent": "aviation-data-analysis (github.com/watanaberyunosuke/motherduck-aviation-data-analysis)"}
OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network"
                     "/protocol/openid-connect/token")
# Live traffic within 500 NM, so en route arrivals and departures show, not just the
# terminal area (merge_live cuts both feeds to the circle). OpenSky gets a 16.6 x 16.6
# degree box (3 credits: 100-400 square degrees).
LIVE_RADIUS_NM = 500
LIVE_BOX_DEG = 8.3
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
        req = urllib.request.Request(OPENSKY_TOKEN_URL, data=body, headers=UA)
        with urllib.request.urlopen(req, timeout=8) as r:
            tok = json.load(r)
        _token = (tok["access_token"], time.time() + int(tok.get("expires_in", 1800)))
    return {"Authorization": f"Bearer {_token[0]}"}


def _live_error(status: int, detail: str) -> JSONResponse:
    # Errors are not edge-cached, so the next viewer retries.
    return JSONResponse({"detail": detail}, status_code=status,
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})


ADSB_LOL = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{nm}"  # ODbL, no key


def _get_json(url: str, headers: dict, timeout: float):
    with urllib.request.urlopen(urllib.request.Request(url, headers={**UA, **headers}),
                                timeout=timeout) as r:
        return json.load(r)


def _from_opensky(lat: float, lon: float) -> list[dict]:
    query = urllib.parse.urlencode({"lamin": lat - LIVE_BOX_DEG, "lamax": lat + LIVE_BOX_DEG,
                                    "lomin": lon - LIVE_BOX_DEG, "lomax": lon + LIVE_BOX_DEG})
    payload = _get_json(f"{OPENSKY_STATES}?{query}", _opensky_auth(), timeout=5)
    # State vector fields: https://openskynetwork.github.io/opensky-api/rest.html
    return [{
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


def _from_adsb_lol(lat: float, lon: float) -> list[dict]:
    payload = _get_json(ADSB_LOL.format(lat=lat, lon=lon, nm=LIVE_RADIUS_NM), {}, timeout=8)
    # readsb JSON: altitudes in ft ("ground" when on the ground), speeds in kt.
    return [{
        "icao24": a.get("hex"),
        "callsign": (a.get("flight") or "").strip() or None,
        "lon": a["lon"],
        "lat": a["lat"],
        "alt_ft": a["alt_baro"] if isinstance(a.get("alt_baro"), (int, float)) else 0,
        "on_ground": a.get("alt_baro") == "ground",
        "speed_kt": None if a.get("gs") is None else round(a["gs"]),
        "track_deg": a.get("track", a.get("true_heading")),
        "vrate_fpm": a.get("baro_rate", a.get("geom_rate")),
    } for a in payload.get("ac") or [] if a.get("lat") is not None and a.get("lon") is not None]


# Direction of each live aircraft relative to the airport, worked out as the Dive does
# (dives/airport_conditions/index.tsx, `placed`), so every client gets the same answer:
# "inbound" / "outbound" from the callsign's last 30 days here, with the track agreeing
# beyond 30 NM and, nearer in, a clear descent or climb deciding for callsigns flown both
# ways; "ground" on the ground within 8 km; otherwise "other". It is the answer for this
# fix alone: clients keep an airborne aircraft's earlier direction until it lands, which
# this stateless function cannot.
DIRECTION_SQL = """
    select distinct callsign, 'inbound' as dir from marts.fct_arrivals
    where arrival_icao = $1 and arrived_at >= now() - interval 30 day and callsign is not null
    union
    select distinct callsign, 'outbound' from marts.fct_departures
    where departure_icao = $1 and departed_at >= now() - interval 30 day and callsign is not null
"""
# Kept per instance as long as the table exports are cached, so the 2-minute live calls do
# not query the warehouse each time.
DIRECTION_TTL_S = 10 * 60
_directions: dict[str, tuple[float, dict[str, set[str]]]] = {}
KM_PER_NM = 1.852


def _callsign_dirs(icao: str) -> dict[str, set[str]]:
    cached = _directions.get(icao)
    if cached and time.time() - cached[0] < DIRECTION_TTL_S:
        return cached[1]
    cur = get_connection().cursor()
    try:
        rows = cur.execute(DIRECTION_SQL, [icao]).fetchall()
    finally:
        cur.close()
    dirs: dict[str, set[str]] = {}
    for callsign, d in rows:
        dirs.setdefault(callsign, set()).add(d)
    _directions[icao] = (time.time(), dirs)
    return dirs


# All-cargo operators (reference.cargo_operators), cached like the directions. Live aircraft
# and schedule rows are tagged with them as the marts' is_freighter column is
# (dbt/macros/is_freighter.sql), so every client shows the same flights as freighters.
_cargo_operators: tuple[float, frozenset[str]] | None = None
_DESIGNATOR = re.compile(r"([A-Z]{3}).")


def _cargo_ops() -> frozenset[str]:
    global _cargo_operators
    if _cargo_operators and time.time() - _cargo_operators[0] < DIRECTION_TTL_S:
        return _cargo_operators[1]
    cur = get_connection().cursor()
    try:
        ops = frozenset(r[0] for r in cur.execute("select icao from reference.cargo_operators").fetchall())
    finally:
        cur.close()
    _cargo_operators = (time.time(), ops)
    return ops


def is_freighter(callsign: str | None, operators: frozenset[str]) -> bool:
    """Flown by an all-cargo operator, matched on the callsign's ICAO designator."""
    m = _DESIGNATOR.match(callsign or "")
    return bool(m) and m.group(1) in operators


def _dist_km(la1: float, lo1: float, la2: float, lo2: float) -> float:
    r = math.pi / 180
    h = (math.sin((la2 - la1) * r / 2) ** 2
         + math.cos(la1 * r) * math.cos(la2 * r) * math.sin((lo2 - lo1) * r / 2) ** 2)
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def _bearing_deg(la1: float, lo1: float, la2: float, lo2: float) -> float:
    r = math.pi / 180
    y = math.sin((lo2 - lo1) * r) * math.cos(la2 * r)
    x = (math.cos(la1 * r) * math.sin(la2 * r)
         - math.sin(la1 * r) * math.cos(la2 * r) * math.cos((lo2 - lo1) * r))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def direction(a: dict, lat: float, lon: float, dirs: dict[str, set[str]]) -> str:
    km = _dist_km(a["lat"], a["lon"], lat, lon)
    if a["on_ground"]:
        return "ground" if km < 8 else "other"
    seen = dirs.get(a["callsign"] or "", set())
    # 0 = heading straight at the airport, 180 = straight away.
    off = abs(((a["track_deg"] or 0) - _bearing_deg(a["lat"], a["lon"], lat, lon) + 540) % 360 - 180)
    near = km < 30 * KM_PER_NM
    vrate = a["vrate_fpm"]
    if "inbound" in seen and "outbound" in seen:
        if near and vrate is not None and abs(vrate) >= 300:
            return "inbound" if vrate < 0 else "outbound"
        return "inbound" if off < 90 else "outbound"
    if "inbound" in seen and (near or off < 110):
        return "inbound"
    if "outbound" in seen and (near or off > 70):
        return "outbound"
    return "other"


# After a source fails, skip it for a while in this instance, so viewers don't wait for
# its timeout on every call (OpenSky times out from Vercel). It is retried afterwards.
SKIP_AFTER_FAILURE_S = 15 * 60
_skip_until: dict[str, float] = {}


def _why(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return "quota used up" if exc.code == 429 else f"HTTP {exc.code}"
    return f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"


# Both sources are asked at once. OpenSky leads (around Hong Kong it saw about twice
# adsb.lol's aircraft); adsb.lol adds the aircraft OpenSky lacks, matched on transponder
# address. If one fails, the other answers alone.
LIVE_SOURCES = (("OpenSky", _from_opensky), ("adsb.lol", _from_adsb_lol))


def merge_live(feeds: list[list[dict] | None], lat: float, lon: float) -> list[dict]:
    """Aircraft within LIVE_RADIUS_NM, each once: from the first feed that has it. OpenSky's
    box reaches past the radius at its corners, so everything is cut to the circle."""
    seen, out = set(), []
    for feed in feeds:
        for a in feed or []:
            # adsb.lol marks non-ICAO addresses with "~"; kept, so they never match one.
            key = (a["icao24"] or "").lower()
            if not key or key in seen:
                continue
            if _dist_km(a["lat"], a["lon"], lat, lon) > LIVE_RADIUS_NM * KM_PER_NM:
                continue
            seen.add(key)
            out.append(a)
    return out


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

    failures, fetched = [], {}
    due = []
    for name, fetch in LIVE_SOURCES:
        if time.time() < _skip_until.get(name, 0):
            failures.append(f"{name}: skipped after a recent failure")
        else:
            due.append((name, fetch))
    with ThreadPoolExecutor(max(len(due), 1)) as pool:
        for (name, _), future in zip(due, [pool.submit(fetch, lat, lon) for _, fetch in due]):
            try:
                fetched[name] = future.result()
            except Exception as exc:  # network, HTTP or payload errors: the other source may answer
                _skip_until[name] = time.time() + SKIP_AFTER_FAILURE_S
                failures.append(f"{name}: {_why(exc)}")
                print(f"live {icao}: {failures[-1]}")
                continue
            _skip_until.pop(name, None)
    if not fetched:
        return _live_error(502, "; ".join(failures))
    aircraft = merge_live([fetched.get(name) for name, _ in LIVE_SOURCES], lat, lon)
    try:
        dirs = _callsign_dirs(icao)
    except duckdb.Error as exc:  # positions are still worth sending without directions
        print(f"live {icao}: no directions: {exc}")
        dirs = None
    try:
        ops = _cargo_ops()
    except duckdb.Error as exc:  # likewise without freighter tags
        print(f"live {icao}: no cargo operators: {exc}")
        ops = None
    for a in aircraft:
        a["dir"] = None if dirs is None else direction(a, lat, lon, dirs)
        a["is_freighter"] = None if ops is None else is_freighter(a["callsign"], ops)
    source = " + ".join(name for name, _ in LIVE_SOURCES if name in fetched)
    return JSONResponse({"time": int(time.time()), "source": source, "aircraft": aircraft,
                         "failed": failures}, headers=LIVE_HEADERS)


# --- Live schedule -------------------------------------------------------------------

# Airline schedules with live status, for the airports that publish them. The warehouse has
# no timetable (OpenSky observes aircraft; AeroDataBox fills only days OpenSky missed), so
# this asks the airport and the edge caches each airport's answer for 3 minutes. Airports
# without a source answer `available: false`, and clients keep predicting from each
# callsign's usual time.
#   VHHH  Airport Authority Hong Kong open data (data.gov.hk "Flight Information"): free,
#         no key. Yesterday's late flights, today and tomorrow, passenger and cargo, with
#         stands for passenger arrivals and gates for passenger departures.
# Not yet: EHAM from Schiphol's Public Flight API (free, needs an app id and key) and the
# other airports from AeroDataBox (paid units).
SCHEDULE_HEADERS = {
    "Cache-Control": "no-cache",
    "Vercel-CDN-Cache-Control": "max-age=180",
    "Access-Control-Allow-Origin": "*",
}
# Flights whose scheduled, estimated or actual time falls in this window around now.
SCHEDULE_PAST = timedelta(hours=6)
SCHEDULE_NEXT = timedelta(hours=18)

HKIA_URL = "https://www.hongkongairport.com/flightinfo-rest/rest/flights"
# Status prefix -> state, and whether the time after it is an estimate or an actual.
# "Boarding Soon" before "Boarding".
HKIA_STATES = (
    ("At gate", "at_gate", "actual"),
    ("Landed", "landed", "actual"),
    ("Dep", "departed", "actual"),
    ("Est at", "estimated", "estimated"),
    ("Cancelled", "cancelled", None),
    ("Delayed", "delayed", None),
    ("Boarding Soon", "boarding_soon", None),
    ("Boarding", "boarding", None),
    ("Final Call", "final_call", None),
    ("Gate Closed", "gate_closed", None),
)
# "15:02", or "23:08 (06/10/2026)" when the day differs from the scheduled one.
_HKIA_TIME = re.compile(r"(\d{1,2}):(\d{2})(?:\s*\((\d{2})/(\d{2})/(\d{4})\))?")
# Operating flight number: two-character IATA airline code, then the number.
_FLIGHT_NO = re.compile(r"([A-Z0-9]{2})\s*0*(\d{1,4})")


def _iso(at: datetime | None) -> str | None:
    return None if at is None else at.astimezone(timezone.utc).isoformat(timespec="seconds")


def _blank(v) -> str | None:
    v = (v or "").strip() if isinstance(v, str) else v
    return v or None


def _hkia_time(text: str, day: date, tz: ZoneInfo) -> datetime | None:
    m = _HKIA_TIME.search(text)
    if not m:
        return None
    hh, mm, dd, mo, yyyy = m.groups()
    on = date(int(yyyy), int(mo), int(dd)) if dd else day
    return datetime(on.year, on.month, on.day, int(hh), int(mm), tzinfo=tz)


def hkia_flights(payload: list[dict], arrival: bool, cargo: bool, tz: ZoneInfo) -> list[dict]:
    """One HKIA board (arrivals or departures, passenger or cargo) as schedule rows."""
    rows = []
    for day_board in payload:
        day = date.fromisoformat(day_board["date"])
        for f in day_board.get("list") or []:
            hh, mm = (int(x) for x in f["time"].split(":"))
            scheduled = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            status = (f.get("status") or "").strip()
            state, estimated, actual = ("scheduled" if not status else "other"), None, None
            for prefix, name, kind in HKIA_STATES:
                if status.startswith(prefix):
                    state = name
                    at = _hkia_time(status[len(prefix):], day, tz) if kind else None
                    estimated, actual = (at, None) if kind == "estimated" else (None, at)
                    break
            numbers = [n["no"].replace(" ", "") for n in f.get("flight") or []]
            if not numbers:
                continue
            operating = f["flight"][0]
            airline = _blank(operating.get("airline"))
            m = _FLIGHT_NO.fullmatch(operating["no"].strip())
            route = f.get("origin" if arrival else "destination") or []
            rows.append({
                "dir": "inbound" if arrival else "outbound",
                "flight": numbers[0],
                "codeshares": numbers[1:],
                # As ADS-B callsigns once leading zeros are dropped: CPA0710 and CPA710 alike.
                "callsign": f"{airline}{m.group(2)}" if airline and len(airline) == 3 and m else None,
                "airline_icao": airline,
                "other": route[0] if route else None,
                "route": route,
                "scheduled_at": _iso(scheduled),
                "estimated_at": _iso(estimated),
                "actual_at": _iso(actual),
                "state": state,
                "status": status or None,
                "stand": _blank(f.get("stand")),
                "gate": _blank(f.get("gate")),
                "belt": _blank(f.get("baggage")),
                "terminal": _blank(f.get("terminal")),
                "cargo": cargo,
            })
    return rows


def tag_freighters(rows: list[dict], operators: frozenset[str]) -> None:
    """Freighter: on the airport's cargo board, or flown by an all-cargo operator. The cargo
    board also lists combination carriers' freighters (a Cathay freighter flies a CPA
    callsign), which no operator list can tell apart from their passenger flights."""
    for r in rows:
        r["is_freighter"] = r["cargo"] or is_freighter(r["callsign"], operators)


def _in_window(row: dict, now: datetime) -> bool:
    times = [datetime.fromisoformat(t) for t in (row["scheduled_at"], row["estimated_at"], row["actual_at"]) if t]
    return any(now - SCHEDULE_PAST <= t <= now + SCHEDULE_NEXT for t in times)


def _hkia(tz: ZoneInfo, now: datetime) -> tuple[list[dict], list[str]]:
    today = now.astimezone(tz).date()
    boards = [(arrival, cargo) for arrival in (True, False) for cargo in (False, True)]

    def fetch(board: tuple[bool, bool]) -> list[dict]:
        arrival, cargo = board
        query = urllib.parse.urlencode({"span": 2, "date": today.isoformat(), "lang": "en",
                                        "cargo": str(cargo).lower(), "arrival": str(arrival).lower()})
        return hkia_flights(_get_json(f"{HKIA_URL}?{query}", UA, timeout=8), arrival, cargo, tz)

    rows, failures = [], []
    with ThreadPoolExecutor(len(boards)) as pool:
        for (arrival, cargo), future in zip(boards, [pool.submit(fetch, b) for b in boards]):
            try:
                rows += future.result()
            except Exception as exc:  # keep the boards that did load
                name = f"{'cargo ' if cargo else ''}{'arrivals' if arrival else 'departures'}"
                failures.append(f"{name}: {_why(exc)}")
    if failures and not rows:
        raise RuntimeError("; ".join(failures))
    return rows, failures


SCHEDULE_SOURCES = {
    "VHHH": ("Airport Authority Hong Kong", _hkia),
}


@app.get("/api/schedule/{icao}")
def schedule(icao: str) -> JSONResponse:
    icao = icao.upper()
    cur = get_connection().cursor()
    try:
        row = cur.execute("select timezone from reference.airports where icao = ?", [icao]).fetchone()
    finally:
        cur.close()
    if row is None:
        return _live_error(404, f"unknown airport {icao!r}")
    now = datetime.now(timezone.utc)
    body = {"time": int(now.timestamp()), "available": False, "source": None, "flights": [], "failed": []}
    if icao not in SCHEDULE_SOURCES:
        return JSONResponse(body, headers=SCHEDULE_HEADERS)
    name, fetch = SCHEDULE_SOURCES[icao]
    try:
        rows, failures = fetch(ZoneInfo(row[0]), now)
    except Exception as exc:
        print(f"schedule {icao}: {exc}")
        return _live_error(502, f"{name}: {exc}")
    rows = sorted((r for r in rows if _in_window(r, now)), key=lambda r: r["scheduled_at"])
    try:
        ops = _cargo_ops()
    except duckdb.Error as exc:
        print(f"schedule {icao}: no cargo operators: {exc}")
        ops = frozenset()
    tag_freighters(rows, ops)
    body.update(available=True, source=name, flights=rows, failed=failures)
    return JSONResponse(body, headers=SCHEDULE_HEADERS)
