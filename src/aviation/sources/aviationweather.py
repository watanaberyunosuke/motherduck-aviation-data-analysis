"""METAR and TAF from the NOAA/NWS Aviation Weather Center Data API.

Docs: https://aviationweather.gov/data/api/  (free, no key, global ICAO coverage)
"""
from __future__ import annotations

import duckdb

from aviation import warehouse
from aviation.http import DEFAULT_TIMEOUT, session

BASE = "https://aviationweather.gov/api/data"


def fetch_metar(icaos: list[str], hours: int = 3) -> list[dict]:
    """Every METAR/SPECI in the last `hours`. Overlapping windows are harmless
    because rows are keyed on (icao, obsTime)."""
    r = session().get(f"{BASE}/metar",
                      params={"ids": ",".join(icaos), "format": "json", "hours": hours},
                      timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json() if r.content else []


def fetch_taf(icaos: list[str]) -> list[dict]:
    r = session().get(f"{BASE}/taf", params={"ids": ",".join(icaos), "format": "json"},
                      timeout=DEFAULT_TIMEOUT)
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


def ingest_metar(con: duckdb.DuckDBPyConnection, icaos: list[str], hours: int = 3) -> int:
    n = warehouse.upsert(con, "raw.metar", metar_rows(fetch_metar(icaos, hours)), ["icao", "obs_time"])
    warehouse.log_run(con, "metar", n)
    return n


def ingest_taf(con: duckdb.DuckDBPyConnection, icaos: list[str]) -> int:
    n = warehouse.upsert(con, "raw.taf", taf_rows(fetch_taf(icaos)), ["icao", "issue_time"])
    warehouse.log_run(con, "taf", n)
    return n
