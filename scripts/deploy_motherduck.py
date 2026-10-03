"""Publish the MotherDuck Flight and Dive from this checkout.

    python scripts/deploy_motherduck.py [--sha SHA] [--only flight|dive ...]

Runs in the ci.yml deploy job on every push to main; can also be run locally with
MOTHERDUCK_TOKEN set. Both objects are matched by name, so the first run creates them and
later runs update them (each update is a new version in MotherDuck).

- Flight `aviation_pipeline` (flights/aviation_pipeline): ingest + dbt build, pinned to
  --sha. The commit must already be on GitHub, because the Flight downloads it. Published
  unscheduled; scripts/run_flight.py starts it hourly from GitHub Actions.
- Flight secret `opensky`: (re)created from OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET when
  both are set, otherwise it must already exist.
- Dive "Airport conditions" (dives/airport_conditions).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("HOME", "/tmp")  # duckdb's extension cache, as in api/index.py

import duckdb

ROOT = Path(__file__).resolve().parents[1]
REPO = "watanaberyunosuke/motherduck-aviation-data-analysis"
DATABASE = "aviation"

FLIGHT_NAME = "aviation_pipeline"
FLIGHT_DIR = ROOT / "flights" / FLIGHT_NAME
# No schedule_cron: MotherDuck only schedules Flights on a Business plan, so the hourly
# trigger is the cron in .github/workflows/ingest.yml (scripts/run_flight.py).
FLIGHT_SECRET = "opensky"
FLIGHT_SECRET_KEYS = ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET")

DIVE_DIR = ROOT / "dives" / "airport_conditions"


# Flight and Dive functions take literals only (no bound parameters), so values are
# inlined as SQL string literals. Single quotes are the only escape a literal needs.
def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_list(values: list[str]) -> str:
    return "[" + ", ".join(sql_str(v) for v in values) + "]"


def sql_map(values: dict[str, str]) -> str:
    return "MAP {" + ", ".join(f"{sql_str(k)}: {sql_str(v)}" for k, v in values.items()) + "}"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True,
                          text=True).stdout.strip()


def ensure_flight_secret(con: duckdb.DuckDBPyConnection) -> None:
    values = {k: os.environ.get(k, "").strip() for k in FLIGHT_SECRET_KEYS}
    if all(values.values()):
        con.execute(f"create or replace secret {FLIGHT_SECRET} in motherduck "
                    f"(type flights, params {sql_map(values)})")
        print(f"flight secret {FLIGHT_SECRET}: updated")
        return
    exists = con.execute("select count(*) from duckdb_secrets() where name = ?",
                         [FLIGHT_SECRET]).fetchone()[0]
    if not exists:
        raise SystemExit(f"Flight secret {FLIGHT_SECRET!r} does not exist. Set "
                         f"{' and '.join(FLIGHT_SECRET_KEYS)} and re-run to create it.")
    print(f"flight secret {FLIGHT_SECRET}: kept existing")


def deploy_flight(con: duckdb.DuckDBPyConnection, sha: str) -> None:
    ensure_flight_secret(con)
    source = ((FLIGHT_DIR / "main.py").read_text()
              .replace("__REPO__", REPO).replace("__GIT_SHA__", sha))
    args = {
        "source_code": sql_str(source),
        "requirements_txt": sql_str((FLIGHT_DIR / "requirements.txt").read_text()),
        "config": sql_map({"WAREHOUSE": f"md:{DATABASE}", "SOURCES": ""}),
        "flight_secret_names": sql_list([FLIGHT_SECRET]),
    }
    named = ", ".join(f"{k} := {v}" for k, v in args.items())
    ids = [r[0] for r in con.execute(
        "select flight_id from md_list_flights() where flight_name = ?", [FLIGHT_NAME]
    ).fetchall()]
    if len(ids) > 1:
        raise SystemExit(f"{len(ids)} Flights are named {FLIGHT_NAME!r}; delete the extras.")
    if ids:
        con.execute(f"call md_update_flight(flight_id := {sql_str(str(ids[0]))}, {named})")
        print(f"flight {FLIGHT_NAME}: updated {ids[0]} to {sha[:7]}")
    else:
        flight_id = con.execute(
            f"select flight_id from md_create_flight(name := {sql_str(FLIGHT_NAME)}, {named})"
        ).fetchone()[0]
        print(f"flight {FLIGHT_NAME}: created {flight_id} at {sha[:7]}")


def deploy_dive(con: duckdb.DuckDBPyConnection, sha: str) -> None:
    meta = json.loads((DIVE_DIR / "dive.metadata.json").read_text())
    content = (DIVE_DIR / "index.tsx").read_text()
    resources = f"[{{'url': {sql_str('md:' + DATABASE)}, 'alias': {sql_str(DATABASE)}}}]"
    ids = [r[0] for r in con.execute(
        "select id from md_list_dives() where title = ?", [meta["title"]]
    ).fetchall()]
    if len(ids) > 1:
        raise SystemExit(f"{len(ids)} Dives are titled {meta['title']!r}; delete the extras.")
    if ids:
        version = con.execute(
            f"select version from md_update_dive_content(id := {sql_str(str(ids[0]))}, "
            f"content := {sql_str(content)}, description := {sql_str(f'Deployed {sha[:7]}')}, "
            f"required_resources := {resources})"
        ).fetchone()[0]
        print(f"dive {meta['title']!r}: updated {ids[0]} to version {version}")
    else:
        dive_id = con.execute(
            f"select id from md_create_dive(title := {sql_str(meta['title'])}, "
            f"description := {sql_str(meta['description'])}, content := {sql_str(content)}, "
            f"required_resources := {resources})"
        ).fetchone()[0]
        print(f"dive {meta['title']!r}: created {dive_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", help="commit the Flight runs (default: HEAD)")
    parser.add_argument("--only", choices=["flight", "dive"], action="append",
                        help="publish only this; repeatable (default: both)")
    args = parser.parse_args()

    sha = git("rev-parse", args.sha or "HEAD")
    steps = {"flight": deploy_flight, "dive": deploy_dive}
    targets = args.only or list(steps)
    if "flight" in targets and not os.environ.get("GITHUB_ACTIONS"):
        if not git("branch", "-r", "--contains", sha):
            raise SystemExit(f"{sha[:7]} is not on GitHub yet; push it first, since the "
                             f"Flight downloads that commit.")

    con = duckdb.connect("md:")
    failed = []
    for name, deploy in steps.items():
        if name in targets:
            # The Dive does not depend on the Flight, so one failing must not skip the other.
            try:
                deploy(con, sha)
            except (duckdb.Error, SystemExit) as exc:
                print(f"{name}: FAILED - {exc}", file=sys.stderr)
                failed.append(name)
    if failed:
        raise SystemExit(f"deploy failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
