"""MotherDuck Flight: scheduled ingest and dbt build for the aviation warehouse.

One Flight runs weather and NOTAM ingest and dbt, on demand: from ingest.yml with
`runner: flight` and as the deploy's smoke test (the free plan cannot schedule Flights).
The hourly schedule runs the same PLAN on the GitHub runner instead, with flights and one
dbt build, because the dbt build outgrew the plan's daily Flight minutes. Running
everything in one Flight means ingest and dbt never write to the warehouse at the same
time. SOURCES='opensky aerodatabox' runs flights here too.

Each run downloads a pinned commit of this repo from GitHub, runs PLAN, then runs
`dbt build`. The commit is written
into the source at deploy time by scripts/deploy_motherduck.py, so per-run config cannot
change which code runs.

Config (Flight config, overridable per run with MD_RUN_FLIGHT(config := MAP {...})):
    WAREHOUSE  dbt / ingest target, e.g. md:aviation
    SOURCES    space-separated sources to run instead of PLAN,
               e.g. 'opensky' or 'metar taf notam-hk notam-faa-search opensky aerodatabox'.
               'none' runs dbt only.
Secret `opensky` (TYPE flights): OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET.
Secret `aerodatabox` (TYPE flights, optional): AERODATABOX_KEY. Without it the AeroDataBox
step is skipped and OpenSky covers all flights.
Secret `faa` (TYPE flights, optional): FAA_CLIENT_ID, FAA_CLIENT_SECRET, for the FAA NOTAM
API fallback (notam-faa). Unused while no airport has notam_source: faa.
MOTHERDUCK_TOKEN is injected by the Flight runtime.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = "__REPO__"        # owner/name, substituted at deploy
GIT_SHA = "__GIT_SHA__"  # commit to run, substituted at deploy


# Every run without SOURCES, and the weather and NOTAM part of the hourly GitHub run
# (ingest.yml). METAR and TAF fetches cover weather.lookback_hours, so a missed run
# leaves no gap.
PLAN = ["metar", "taf", "wx-extra", "atis-hk", "procedures", "notam-hk", "notam-faa-search"]


def pick_sources(override: str) -> list[str]:
    override = override.strip()
    if not override:
        return list(PLAN)
    if override == "none":
        return []
    return override.split()


def download_repo(repo: str, sha: str, dest: Path) -> Path:
    url = f"https://codeload.github.com/{repo}/tar.gz/{sha}"
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = resp.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        # The "data" filter rejects absolute paths and links outside dest.
        kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(dest, **kwargs)
    (root,) = [p for p in dest.iterdir() if p.is_dir()]
    return root


def run_dbt(project_dir: Path) -> bool:
    # A separate process, not dbtRunner: ingest's md: connection leaves a cached database
    # instance in this process after close(), and dbt-duckdb connects to the same database
    # with different config (TimeZone etc.), which DuckDB refuses for a shared instance.
    args = ["build", "--project-dir", str(project_dir), "--profiles-dir", str(project_dir)]
    print(f"dbt {' '.join(args)}", flush=True)
    cmd = [sys.executable, "-c", "from dbt.cli.main import cli; cli()", *args]
    return subprocess.run(cmd).returncode == 0


def main() -> None:
    # duckdb needs $HOME for its extension cache before it will attach md: targets.
    os.environ.setdefault("HOME", "/tmp")
    os.environ.setdefault("WAREHOUSE", "md:aviation")
    os.environ.setdefault("DO_NOT_TRACK", "1")

    sources = pick_sources(os.environ.get("SOURCES", ""))
    print(f"commit {GIT_SHA}, sources: {' '.join(sources) or '(none)'}")

    with tempfile.TemporaryDirectory() as tmp:
        root = download_repo(REPO, GIT_SHA, Path(tmp))
        sys.path.insert(0, str(root / "src"))
        from aviation.cli import run as ingest

        failures = 0
        for source in sources:
            # One source failing must not stop the others or the dbt build.
            failures += ingest(source)

        # Rebuild marts even if a source failed, so the others still land.
        if not run_dbt(root / "dbt"):
            failures += 1

    if failures:
        print(f"{failures} step(s) failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
