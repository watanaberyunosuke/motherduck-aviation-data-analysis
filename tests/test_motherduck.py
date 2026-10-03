"""The MotherDuck Flight's source plan and the deploy script's SQL quoting."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


flight = _load("flight_main", ROOT / "flights" / "aviation_pipeline" / "main.py")
deploy = _load("deploy_motherduck", ROOT / "scripts" / "deploy_motherduck.py")


@pytest.mark.parametrize("hour, expected", [
    (1, ["metar", "taf"]),
    (3, ["metar", "taf", "notam-hk"]),
    (0, ["metar", "taf", "notam-hk"]),
    (6, ["metar", "taf", "notam-hk", "opensky"]),
    (7, ["metar", "taf"]),
])
def test_hourly_plan_matches_old_ingest_schedule(hour, expected):
    assert flight.pick_sources("", hour) == expected


def test_sources_override():
    assert flight.pick_sources(" opensky ", 1) == ["opensky"]
    assert flight.pick_sources("none", 6) == []


def test_every_source_in_plan_is_a_cli_source():
    import inspect

    import aviation.cli as cli

    cli_choices = inspect.getsource(cli.main)
    for hour in range(24):
        for source in flight.sources_for_hour(hour):
            assert f'"{source}"' in cli_choices


@pytest.mark.parametrize("value", [
    "plain",
    "it's got 'quotes' and ''doubles''",
    'back\\slash \\n and "double" quotes',
    "$$ dollar $tag$ and\nnewlines\n",
    (ROOT / "flights" / "aviation_pipeline" / "main.py").read_text(),
    (ROOT / "dives" / "airport_conditions" / "index.tsx").read_text(),
])
def test_sql_str_round_trips(value):
    assert duckdb.sql(f"select {deploy.sql_str(value)}").fetchone()[0] == value


def test_sql_map_and_list():
    m = duckdb.sql(f"select {deploy.sql_map({'A': '1', 'B': ''})}").fetchone()[0]
    assert m == {"A": "1", "B": ""}
    assert duckdb.sql(f"select {deploy.sql_list(['x', 'y'])}").fetchone()[0] == ["x", "y"]


def test_flight_placeholders_present():
    source = (ROOT / "flights" / "aviation_pipeline" / "main.py").read_text()
    assert 'REPO = "__REPO__"' in source
    assert 'GIT_SHA = "__GIT_SHA__"' in source
