"""Vercel function: hands the browser the warehouse tables the Airport conditions page needs.

The page (web/, built from the MotherDuck Dive) runs its SQL in the browser on
DuckDB-WASM. This function is the only part that talks to MotherDuck, so the token stays
server-side: it exports each allow-listed table as Parquet, and Vercel's edge caches the
file for 10 minutes. The pipeline lands new data hourly, so that is fresh enough.

Self-contained on purpose: no import of the `aviation` package. Reads only, never writes.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Vercel's Python sandbox doesn't set $HOME, which duckdb needs (for its extension
# cache, the motherduck extension in particular) before it will attach an md: target.
os.environ.setdefault("HOME", "/tmp")

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

app = FastAPI()

# Every table the Dive queries, with the rows it can need. Windows must cover the
# Dive's own date filters (30 days at most, except the all-time weather penalty on
# fct_arrival_weather_impact); they keep each file well under Vercel's 4.5 MB
# response limit as history grows.
TABLES = {
    "reference.airports": "true",
    "marts.fct_airport_weather_hourly": "hour_utc >= now() - interval 31 day",
    "marts.fct_daily_airport_movements": "day_utc >= current_date - 31",
    "marts.fct_arrivals": "arrived_at >= now() - interval 31 day",
    "marts.fct_arrival_weather_impact": "true",
}

CACHE = "public, max-age=0, s-maxage=600, stale-while-revalidate=3600"

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
    return Response(body, media_type="application/vnd.apache.parquet",
                    headers={"Cache-Control": CACHE})
