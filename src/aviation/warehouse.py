"""DuckDB / MotherDuck raw layer.

Every raw table keeps the full source payload as JSON next to its natural key, so dbt
can re-derive anything later. Loads are idempotent: re-running a fetch overwrites the
same keys rather than duplicating them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
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


def upsert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict], key: list[str],
           keep_on_conflict: tuple[str, ...] = ()) -> int:
    """Insert rows; on key conflict overwrite every column except `keep_on_conflict`."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in cols if c not in key and c not in keep_on_conflict
    )
    sql = (
        f"insert into {table} ({', '.join(cols)}) values ({placeholders}) "
        f"on conflict ({', '.join(key)}) do update set {updates}"
    )
    con.executemany(sql, [[_to_db(r[c]) for c in cols] for r in rows])
    return len(rows)


def log_run(con: duckdb.DuckDBPyConnection, source: str, rows: int, detail: str = "") -> None:
    con.execute("insert into raw.ingest_log values (?, ?, ?, ?)", [utcnow(), source, rows, detail])


def _to_db(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value
