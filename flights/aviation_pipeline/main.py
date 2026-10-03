"""MotherDuck Flight: scheduled ingest and dbt build for the aviation warehouse.

One Flight runs the whole pipeline hourly. Running everything in one Flight means ingest
and dbt never write to the warehouse at the same time; that was what the GitHub Actions
concurrency group did before.

Each run downloads a pinned commit of this repo from GitHub, runs the ingest sources due
this hour (the same plan ingest.yml used), then runs `dbt build`. The commit is written
into the source at deploy time by scripts/deploy_motherduck.py, so per-run config cannot
change which code runs.

Config (Flight config, overridable per run with MD_RUN_FLIGHT(config := MAP {...})):
    WAREHOUSE  dbt / ingest target, e.g. md:aviation
    SOURCES    space-separated sources to run instead of this hour's plan,
               e.g. 'opensky' or 'metar taf notam-hk opensky'. 'none' runs dbt only.
Secret `opensky` (TYPE flights): OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET.
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
from datetime import datetime, timezone
from pathlib import Path

REPO = "__REPO__"        # owner/name, substituted at deploy
GIT_SHA = "__GIT_SHA__"  # commit to run, substituted at deploy


def sources_for_hour(hour: int) -> list[str]:
    """The schedule ingest.yml used, folded into one hourly run at :07 UTC."""
    sources = ["metar", "taf"]
    if hour % 3 == 0:
        sources.append("notam-hk")
    if hour == 6:
        sources.append("opensky")  # flights + tracks for yesterday; slowest, so last
    return sources


def pick_sources(override: str, hour: int) -> list[str]:
    override = override.strip()
    if not override:
        return sources_for_hour(hour)
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

    hour = datetime.now(timezone.utc).hour
    sources = pick_sources(os.environ.get("SOURCES", ""), hour)
    print(f"commit {GIT_SHA}, UTC hour {hour}, sources: {' '.join(sources) or '(none)'}")

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
