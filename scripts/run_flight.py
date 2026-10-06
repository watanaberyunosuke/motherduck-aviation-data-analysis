"""Run a Flight on demand and wait for it to finish.

    python scripts/run_flight.py [--sources "metar taf"] [--timeout-minutes 25]
    python scripts/run_flight.py --flight initial_load [--config STEPS=weather ...]

Called by .github/workflows/ingest.yml (`runner: flight`) and the ci.yml deploy job's
smoke test. MotherDuck only schedules Flights on paid plans and this account is on the
free plan, so GitHub Actions starts each run instead. Waiting for the run (rather than fire-and-forget) means a failed
Flight fails the workflow, and the warehouse-writer concurrency group covers the Flight as
well as the deploy job.

--sources is passed as the Flight's SOURCES config; empty runs the Flight's PLAN
(flights/aviation_pipeline/main.py), 'none' runs dbt only. --config KEY=VALUE sets any
other config for the run (repeatable), e.g. initial_load's STEPS or DAYS.

Exits with QUOTA_SPENT (75) when the plan's daily Flight minutes are used up, so the
workflows can tell that apart from a failed run: ci.yml skips its smoke test and ingest.yml
runs the same sources on the GitHub runner instead.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

os.environ.setdefault("HOME", "/tmp")  # duckdb's extension cache, as in api/index.py

import duckdb

from deploy_motherduck import FLIGHT_NAME, FLIGHTS, sql_map, sql_str

POLL_SECONDS = 15
# MotherDuck has reported run status both as RUN_STATUS_SUCCEEDED and as plain
# SUCCEEDED; compare without the prefix so either form ends the wait.
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
QUOTA_SPENT = 75  # EX_TEMPFAIL: the plan's daily Flight minutes are spent (reset at 00:00 UTC)


def normalise(status: object) -> str:
    return str(status).upper().removeprefix("RUN_STATUS_")


def flight_id(con: duckdb.DuckDBPyConnection, name: str) -> str:
    ids = [str(r[0]) for r in con.execute(
        "select flight_id from md_list_flights() where flight_name = ?", [name]
    ).fetchall()]
    if len(ids) != 1:
        raise SystemExit(f"expected one Flight named {name!r}, found {len(ids)}; "
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
    parser.add_argument("--flight", choices=list(FLIGHTS), default=FLIGHT_NAME)
    parser.add_argument("--sources", default="", help="SOURCES override for this run")
    parser.add_argument("--config", action="append", default=[], metavar="KEY=VALUE",
                        help="other config for this run; repeatable")
    parser.add_argument("--timeout-minutes", type=float, default=25)
    args = parser.parse_args()

    config = dict(FLIGHTS[args.flight])
    if args.flight == FLIGHT_NAME:
        config["SOURCES"] = args.sources.strip()
    for item in args.config:
        key, sep, value = item.partition("=")
        if not sep or key not in config:
            raise SystemExit(f"--config {item!r}: expected KEY=VALUE with KEY in {sorted(config)}")
        config[key] = value

    con = duckdb.connect("md:")
    fid = flight_id(con, args.flight)
    try:
        run_number, status = con.execute(
            f"select run_number, status from md_run_flight(flight_id := {sql_str(fid)}, "
            f"config := {sql_map(config)})"
        ).fetchone()
    except duckdb.PermissionException as e:
        # "Your organization has used all 30 minutes of Flight run time included in your
        # plan for today." Nothing was started.
        if "Flight run time" not in str(e):
            raise
        print(f"flight {args.flight}: not started: {e}", file=sys.stderr)
        sys.exit(QUOTA_SPENT)
    print(f"flight {args.flight}: started run {run_number} ({config})")

    exit_code = None
    deadline = time.monotonic() + args.timeout_minutes * 60
    while normalise(status) not in TERMINAL:
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
    print(f"flight {args.flight}: run {run_number} {status}, exit code {exit_code}")
    if normalise(status) != "SUCCEEDED":
        sys.exit(1)


if __name__ == "__main__":
    main()
