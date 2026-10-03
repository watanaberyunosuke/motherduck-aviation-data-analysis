"""Run the aviation_pipeline Flight on demand and wait for it to finish.

    python scripts/run_flight.py [--sources "metar taf"] [--timeout-minutes 25]

The hourly trigger for the Flight, called by .github/workflows/ingest.yml. MotherDuck only
schedules Flights on a Business plan, so GitHub's cron starts each run instead. Waiting for
the run (rather than fire-and-forget) means a failed Flight fails the workflow, and the
warehouse-writer concurrency group covers the Flight as well as the deploy job.

--sources is passed as the Flight's SOURCES config; empty lets the Flight pick this hour's
plan (flights/aviation_pipeline/main.py), 'none' runs dbt only.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

os.environ.setdefault("HOME", "/tmp")  # duckdb's extension cache, as in api/index.py

import duckdb

from deploy_motherduck import FLIGHT_NAME, sql_map, sql_str

POLL_SECONDS = 15
TERMINAL = {"RUN_STATUS_SUCCEEDED", "RUN_STATUS_FAILED", "RUN_STATUS_CANCELLED"}


def flight_id(con: duckdb.DuckDBPyConnection) -> str:
    ids = [str(r[0]) for r in con.execute(
        "select flight_id from md_list_flights() where flight_name = ?", [FLIGHT_NAME]
    ).fetchall()]
    if len(ids) != 1:
        raise SystemExit(f"expected one Flight named {FLIGHT_NAME!r}, found {len(ids)}; "
                         f"deploy it with scripts/deploy_motherduck.py")
    return ids[0]


def print_logs(con: duckdb.DuckDBPyConnection, fid: str, run_number: int) -> None:
    # Flight functions take literals only, so the arguments are inlined.
    for (line,) in con.execute(
        f"select line from md_get_flight_logs(flight_id := {sql_str(fid)}, "
        f"run_number := {int(run_number)}) order by line_number"
    ).fetchall():
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", default="", help="SOURCES override for this run")
    parser.add_argument("--timeout-minutes", type=float, default=25)
    args = parser.parse_args()

    con = duckdb.connect("md:")
    fid = flight_id(con)
    run_number, status = con.execute(
        f"select run_number, status from md_run_flight(flight_id := {sql_str(fid)}, "
        f"config := {sql_map({'SOURCES': args.sources.strip()})})"
    ).fetchone()
    print(f"flight {FLIGHT_NAME}: started run {run_number} "
          f"(sources: {args.sources.strip() or 'hourly plan'})")

    exit_code = None
    deadline = time.monotonic() + args.timeout_minutes * 60
    while status not in TERMINAL:
        if time.monotonic() > deadline:
            print_logs(con, fid, run_number)
            raise SystemExit(f"run {run_number} still {status} after "
                             f"{args.timeout_minutes:g} min; it keeps running in MotherDuck")
        time.sleep(POLL_SECONDS)
        status, exit_code = con.execute(
            f"select status, exit_code from md_list_flight_runs(flight_id := {sql_str(fid)}) "
            f"where run_number = {int(run_number)}"
        ).fetchone()

    print_logs(con, fid, run_number)
    print(f"flight {FLIGHT_NAME}: run {run_number} {status}, exit code {exit_code}")
    if status != "RUN_STATUS_SUCCEEDED":
        sys.exit(1)


if __name__ == "__main__":
    main()
