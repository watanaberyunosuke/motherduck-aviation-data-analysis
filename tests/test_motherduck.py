"""The MotherDuck Flight's source plan and the deploy script's SQL quoting."""
from __future__ import annotations

import importlib.util
import sys
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



class _FakeMotherDuck:
    """Records SQL and answers the Flight functions from canned rows."""

    def __init__(self, flight_ids=("abc",), statuses=()):
        self.flight_ids = list(flight_ids)
        self.statuses = list(statuses)  # successive md_list_flight_runs answers
        self.calls: list[str] = []

    def execute(self, sql, params=None):
        self.calls.append(sql)
        if "md_list_flights" in sql:
            return _Rows([(i,) for i in self.flight_ids])
        if "md_run_flight" in sql:
            return _Rows([(7, "RUN_STATUS_PENDING")])
        if "md_list_flight_runs" in sql:
            return _Rows([self.statuses.pop(0)])
        if "md_get_flight_logs" in sql:
            return _Rows([("log line",)])
        return _Rows([("new-id",)])


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


@pytest.mark.parametrize("existing, verb", [((), "md_create_flight"), (("abc",), "md_update_flight")])
def test_flight_is_published_unscheduled(monkeypatch, existing, verb):
    # Scheduled Flights need a Business plan; GitHub Actions triggers the runs instead.
    monkeypatch.setenv("OPENSKY_CLIENT_ID", "id")
    monkeypatch.setenv("OPENSKY_CLIENT_SECRET", "secret")
    con = _FakeMotherDuck(existing)
    deploy.deploy_flight(con, "0" * 40)
    published = [c for c in con.calls if verb in c]
    assert len(published) == 1
    assert "schedule_cron" not in published[0]


sys.path.insert(0, str(ROOT / "scripts"))
run_flight = _load("run_flight", ROOT / "scripts" / "run_flight.py")


@pytest.mark.parametrize("final, ok", [
    (("RUN_STATUS_SUCCEEDED", 0), True),
    (("RUN_STATUS_FAILED", 1), False),
    (("RUN_STATUS_CANCELLED", None), False),
])
def test_run_flight_waits_and_reports(monkeypatch, capsys, final, ok):
    con = _FakeMotherDuck(statuses=[("RUN_STATUS_RUNNING", None), final])
    monkeypatch.setattr(run_flight.duckdb, "connect", lambda _: con)
    monkeypatch.setattr(run_flight.time, "sleep", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["run_flight.py", "--sources", "opensky"])
    if ok:
        run_flight.main()
    else:
        with pytest.raises(SystemExit):
            run_flight.main()
    (run_sql,) = [c for c in con.calls if "md_run_flight" in c]
    assert "'SOURCES': 'opensky'" in run_sql
    assert "log line" in capsys.readouterr().out
