"""DuckDB / MotherDuck raw layer.

Every raw table keeps the full source payload as JSON next to its natural key, so dbt
can re-derive anything later. Loads are idempotent: re-running a fetch overwrites the
same keys rather than duplicating them.
"""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

DDL = """
create schema if not exists raw;

create table if not exists raw.metar (
    icao        varchar  not null,
    obs_time    bigint   not null,   -- unix seconds, from the payload's obsTime
    fetched_at  timestamptz not null,
    payload     json     not null,
    primary key (icao, obs_time)
);

create table if not exists raw.taf (
    icao        varchar  not null,
    issue_time  varchar  not null,   -- ISO string as published
    fetched_at  timestamptz not null,
    payload     json     not null,
    primary key (icao, issue_time)
);

-- One row per NOTAM per source. first_seen_at / last_seen_at record when the NOTAM
-- appeared in and was last present in the feed, which is how cancellations and
-- replacements become visible even when the source only publishes the active list.
-- Temperature, dew point and weather text from outside the METAR (sources/wx_extra.py):
-- one row per source (gov | open-meteo | met.no) per observation time.
create table if not exists raw.wx_extra (
    icao        varchar not null,
    source      varchar not null,
    observed_at timestamptz not null,
    fetched_at  timestamptz not null,
    temp_c      double,
    dewpoint_c  double,
    wx_text     varchar,
    payload     json not null,
    primary key (icao, source, observed_at)
);

-- Sunrise and sunset for the airport's local day; source is met.no or computed.
create table if not exists raw.sun_times (
    icao       varchar not null,
    day        date not null,
    source     varchar not null,
    sunrise    timestamptz,
    sunset     timestamptz,
    fetched_at timestamptz not null,
    primary key (icao, day)
);

-- Each distinct ATIS broadcast text once (sources/atis_hk.py), with when it was first and
-- last seen on the page.
create table if not exists raw.atis (
    icao          varchar not null,
    text_hash     varchar not null,
    info_letter   varchar,
    text          varchar not null,
    first_seen_at timestamptz not null,
    last_seen_at  timestamptz not null,
    primary key (icao, text_hash)
);

create table if not exists raw.notam (
    source         varchar not null,  -- hk_cad | rapidapi
    notam_key      varchar not null,  -- source-stable id, e.g. VHHK:A2255/26
    number         varchar,
    location       varchar,
    notam_type     varchar,           -- N (new) | R (replace) | C (cancel)
    replaces       varchar,
    fir            varchar,
    q_code         varchar,
    traffic        varchar,
    purpose        varchar,
    scope          varchar,
    lower_fl       integer,
    upper_fl       integer,
    centre_lat     double,
    centre_lon     double,
    radius_nm      integer,
    starts_at      timestamptz,
    ends_at        timestamptz,
    is_permanent   boolean,
    is_estimated   boolean,
    schedule       varchar,
    body           varchar,
    lower_limit    varchar,
    upper_limit    varchar,
    raw_text       varchar,
    first_seen_at  timestamptz not null,
    last_seen_at   timestamptz not null,
    payload        json not null,
    primary key (source, notam_key)
);

create table if not exists raw.opensky_flights (
    icao24       varchar not null,
    first_seen   bigint  not null,
    last_seen    bigint,
    callsign     varchar,
    est_departure_airport varchar,
    est_arrival_airport   varchar,
    fetched_at   timestamptz not null,
    payload      json not null,
    primary key (icao24, first_seen)
);

create table if not exists raw.opensky_tracks (
    icao24      varchar not null,
    start_time  bigint  not null,
    end_time    bigint,
    callsign    varchar,
    fetched_at  timestamptz not null,
    payload     json not null,
    primary key (icao24, start_time)
);

-- AeroDataBox airport departures/arrivals (FIDS). One row per flight per airport board, so
-- a flight between two in-scope airports appears on both; staging merges them by
-- flight_id. The epochs are parsed at load so the OpenSky track selection can use them.
create table if not exists raw.aerodatabox_flights (
    airport_icao    varchar not null,  -- the board it came from
    direction       varchar not null,  -- arrival | departure
    flight_id       varchar not null,  -- number (or callsign, reg) @ scheduled departure UTC
    icao24          varchar,           -- Mode-S hex, lower case
    callsign        varchar,
    departure_epoch bigint,            -- runway, else revised, else scheduled (unix seconds)
    arrival_epoch   bigint,
    status          varchar,
    fetched_at      timestamptz not null,
    payload         json not null,
    primary key (airport_icao, direction, flight_id)
);

-- Which (source, airport, direction, UTC day) flight slots have been fetched, including
-- those that returned nothing. Backfills skip done slots, and staging prefers OpenSky for
-- any slot it has loaded flights for.
create table if not exists raw.flight_slots (
    source      varchar not null,  -- aerodatabox | opensky
    icao        varchar not null,
    direction   varchar not null,  -- arrival | departure
    day_utc     date    not null,
    rows        integer not null,
    fetched_at  timestamptz not null,
    primary key (source, icao, direction, day_utc)
);

create table if not exists raw.ingest_log (
    run_at     timestamptz not null,
    source     varchar not null,
    rows       integer not null,
    detail     varchar
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def connect(target: str) -> duckdb.DuckDBPyConnection:
    if not target.startswith("md:"):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(target)
    con.execute(DDL)
    return con


# Above this many rows, upsert loads through a temporary file in one statement instead of
# one insert per row, which is far too slow against MotherDuck for a backfill.
BULK_THRESHOLD = 50


def upsert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict], key: list[str],
           keep_on_conflict: tuple[str, ...] = (), overwrite: bool = True) -> int:
    """Insert rows; on key conflict overwrite every column except `keep_on_conflict`.

    With overwrite=False, rows whose key already exists are left alone: a lower-priority
    source (a backfill archive) fills gaps without replacing the primary source's rows.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in cols if c not in key and c not in keep_on_conflict
    )
    action = f"do update set {updates}" if overwrite and updates else "do nothing"
    conflict = f"on conflict ({', '.join(key)}) {action}"
    if len(rows) <= BULK_THRESHOLD:
        placeholders = ", ".join("?" for _ in cols)
        con.executemany(f"insert into {table} ({', '.join(cols)}) values ({placeholders}) {conflict}",
                        [[_to_db(r[c]) for c in cols] for r in rows])
        return len(rows)

    types = dict(con.execute(f"select column_name, column_type from (describe {table})").fetchall())
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rows.ndjson"
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps({c: _to_json(r[c]) for c in cols}) + "\n")
        # JSON payloads travel as strings and are cast back, so they keep their exact text.
        spec = ", ".join(f"'{c}': '{'VARCHAR' if types[c] == 'JSON' else types[c]}'" for c in cols)
        select = ", ".join(f"cast({c} as json)" if types[c] == "JSON" else c for c in cols)
        # A key repeated within one batch would make the insert fail, so keep one copy.
        con.execute(f"""
            insert into {table} ({', '.join(cols)})
            select {select} from read_json('{path}', format = 'newline_delimited', columns = {{{spec}}})
            qualify row_number() over (partition by {', '.join(key)} order by 1) = 1
            {conflict}
        """)
    return len(rows)


# OpenSky publishes a UTC day's flights in a nightly batch, and late arrivals land in the
# early hours, so a day only counts as complete from this hour (UTC) the next day.
DAY_READY_HOUR_UTC = 6


def newest_complete_day(days_back: int = 1, now: datetime | None = None) -> int:
    """Days back to the newest flights day that can be loaded for good: `days_back`
    (1 = yesterday) once it is DAY_READY_HOUR_UTC, else one more."""
    now = now or utcnow()
    return days_back if now.hour >= DAY_READY_HOUR_UTC else days_back + 1


def mark_slot(con: duckdb.DuckDBPyConnection, source: str, icao: str, direction: str,
              day: date, rows: int) -> None:
    upsert(con, "raw.flight_slots", [{
        "source": source, "icao": icao, "direction": direction, "day_utc": day,
        "rows": rows, "fetched_at": utcnow(),
    }], ["source", "icao", "direction", "day_utc"])


def log_run(con: duckdb.DuckDBPyConnection, source: str, rows: int, detail: str = "") -> None:
    con.execute("insert into raw.ingest_log values (?, ?, ?, ?)", [utcnow(), source, rows, detail])


def _to_db(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _to_json(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
