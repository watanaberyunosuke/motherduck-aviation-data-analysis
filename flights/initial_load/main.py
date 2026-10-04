"""MotherDuck Flight: one-off history load for the aviation warehouse.

Run on demand (never scheduled) to load history inside MotherDuck, close to the
warehouse, instead of from a laptop where every write is a round trip:

- weather: `aviation backfill weather`: METARs for DAYS (AWC for the last 30, the IEM
  archive before that) and 30 days of TAFs.
- flights: `aviation backfill flights`: AeroDataBox (if its key is set) then OpenSky,
  newest day first, back to each source's backfill_days in config/airports.yml. OpenSky's
  daily credits allow about a week of all seven airports per run; re-run on later days,
  or let the daily flights run on GitHub (ingest.yml) finish the year.

Then `dbt build`. Every step is idempotent, so a run that stops part-way (a Flight run
is capped at an hour) can simply be run again.

Like aviation_pipeline, each run downloads a pinned commit of this repo from GitHub.

Config (per run with MD_RUN_FLIGHT(config := MAP {...})):
    WAREHOUSE          target, e.g. md:aviation
    STEPS              space-separated: weather flights (default both)
    DAYS               weather history in days (default: weather.backfill_days, 3 years)
    OPENSKY_CALLS      OpenSky backfill calls this run (default 1000; the credit floor
                       stops it first)
    AERODATABOX_CALLS  AeroDataBox backfill calls this run (paid units; default from
                       config/airports.yml)
    DBT                'false' to skip dbt build
Secrets: `opensky` (required for flights), `aerodatabox` (optional), as for aviation_pipeline.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = "__REPO__"        # owner/name, substituted at deploy
GIT_SHA = "__GIT_SHA__"  # commit to run, substituted at deploy

STEPS = ("weather", "flights")


def pick_steps(value: str) -> list[str]:
    steps = value.split() or list(STEPS)
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        raise SystemExit(f"unknown STEPS {unknown}; choose from {' '.join(STEPS)}")
    return steps


def optional_int(value: str) -> int | None:
    return int(value) if value.strip() else None


def download_repo(repo: str, sha: str, dest: Path) -> Path:
    url = f"https://codeload.github.com/{repo}/tar.gz/{sha}"
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = resp.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(dest, **kwargs)
    (root,) = [p for p in dest.iterdir() if p.is_dir()]
    return root


def run_dbt(project_dir: Path) -> bool:
    # A separate process, as in aviation_pipeline: dbt-duckdb cannot share the ingest's
    # cached md: database instance.
    args = ["build", "--project-dir", str(project_dir), "--profiles-dir", str(project_dir)]
    print(f"dbt {' '.join(args)}", flush=True)
    cmd = [sys.executable, "-c", "from dbt.cli.main import cli; cli()", *args]
    return subprocess.run(cmd).returncode == 0


def main() -> None:
    os.environ.setdefault("HOME", "/tmp")
    os.environ.setdefault("WAREHOUSE", "md:aviation")
    os.environ.setdefault("DO_NOT_TRACK", "1")

    steps = pick_steps(os.environ.get("STEPS", ""))
    days = optional_int(os.environ.get("DAYS", ""))
    opensky_calls = optional_int(os.environ.get("OPENSKY_CALLS", "") or "1000")
    adb_calls = optional_int(os.environ.get("AERODATABOX_CALLS", ""))
    print(f"commit {GIT_SHA}, steps: {' '.join(steps)}")

    with tempfile.TemporaryDirectory() as tmp:
        root = download_repo(REPO, GIT_SHA, Path(tmp))
        sys.path.insert(0, str(root / "src"))
        import logging

        from aviation.cli import run

        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        failures = 0
        for step in steps:
            started = time.monotonic()
            failures += run(f"backfill-{step}", days, opensky_calls, adb_calls)
            print(f"{step}: {time.monotonic() - started:.0f}s", flush=True)

        if os.environ.get("DBT", "true").lower() != "false" and not run_dbt(root / "dbt"):
            failures += 1

    if failures:
        print(f"{failures} step(s) failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
