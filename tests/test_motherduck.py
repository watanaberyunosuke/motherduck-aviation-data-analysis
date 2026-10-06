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


def test_scheduled_plan_is_weather_and_notams():
    assert flight.pick_sources("") == ["metar", "taf", "wx-extra", "atis-hk", "procedures", "notam-hk", "notam-faa-search"]


def test_sources_override():
    assert flight.pick_sources(" opensky ") == ["opensky"]
    assert flight.pick_sources("none") == []


def test_every_source_in_plan_is_a_cli_source():
    import inspect

    import aviation.cli as cli

    cli_choices = inspect.getsource(cli.main)
    for source in flight.PLAN:
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


@pytest.mark.parametrize("name", list(deploy.FLIGHTS))
def test_flight_placeholders_present(name):
    source = (ROOT / "flights" / name / "main.py").read_text()
    assert 'REPO = "__REPO__"' in source
    assert 'GIT_SHA = "__GIT_SHA__"' in source
    assert (ROOT / "flights" / name / "requirements.txt").exists()


initial_load = _load("initial_load_main", ROOT / "flights" / "initial_load" / "main.py")


def test_initial_load_steps():
    assert initial_load.pick_steps("") == ["weather", "flights"]
    assert initial_load.pick_steps(" flights ") == ["flights"]
    with pytest.raises(SystemExit):
        initial_load.pick_steps("weather opensky")


def test_initial_load_steps_are_cli_backfills():
    import inspect

    import aviation.cli as cli

    source = inspect.getsource(cli.main)
    for step in initial_load.STEPS:
        assert f'"{step}"' in source



class _FakeMotherDuck:
    """Records SQL and answers the Flight functions from canned rows."""

    def __init__(self, flight_ids=("abc",), statuses=(), secrets=("opensky", "faa")):
        self.flight_ids = list(flight_ids)
        self.secrets = set(secrets)  # secrets that already exist in MotherDuck
        self.statuses = list(statuses)  # successive md_list_flight_runs answers
        self.calls: list[str] = []

    def execute(self, sql, params=None):
        self.calls.append(sql)
        if "duckdb_secrets" in sql:
            return _Rows([(int(params[0] in self.secrets),)])
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
    # Scheduled Flights need a paid plan (this account is on the free plan); GitHub Actions
    # triggers the runs instead.
    monkeypatch.setenv("OPENSKY_CLIENT_ID", "id")
    monkeypatch.setenv("OPENSKY_CLIENT_SECRET", "secret")
    con = _FakeMotherDuck(existing)
    deploy.deploy_flights(con, "0" * 40, [deploy.FLIGHT_NAME])
    published = [c for c in con.calls if verb in c]
    assert len(published) == 1
    assert "schedule_cron" not in published[0]


def test_faa_secret_is_optional(monkeypatch):
    monkeypatch.delenv("AERODATABOX_KEY", raising=False)
    monkeypatch.setenv("OPENSKY_CLIENT_ID", "id")
    monkeypatch.setenv("OPENSKY_CLIENT_SECRET", "secret")
    monkeypatch.delenv("FAA_CLIENT_ID", raising=False)
    monkeypatch.delenv("FAA_CLIENT_SECRET", raising=False)
    con = _FakeMotherDuck(secrets=())
    deploy.deploy_flights(con, "0" * 40, [deploy.FLIGHT_NAME])
    (published,) = [c for c in con.calls if "md_update_flight" in c]
    assert "flight_secret_names := ['opensky']" in published

    monkeypatch.setenv("FAA_CLIENT_ID", "fid")
    monkeypatch.setenv("FAA_CLIENT_SECRET", "fsecret")
    con = _FakeMotherDuck(secrets=())
    deploy.deploy_flights(con, "0" * 40, [deploy.FLIGHT_NAME])
    (published,) = [c for c in con.calls if "md_update_flight" in c]
    assert "flight_secret_names := ['opensky', 'faa']" in published


def test_opensky_secret_is_required(monkeypatch):
    for k in ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):
        deploy.deploy_flights(_FakeMotherDuck(secrets=()), "0" * 40, [deploy.FLIGHT_NAME])


sys.path.insert(0, str(ROOT / "scripts"))
run_flight = _load("run_flight", ROOT / "scripts" / "run_flight.py")


@pytest.mark.parametrize("final, ok", [
    (("RUN_STATUS_SUCCEEDED", 0), True),
    (("RUN_STATUS_FAILED", 1), False),
    (("RUN_STATUS_CANCELLED", None), False),
    # The unprefixed form MotherDuck returned on 2026-10-04 must end the wait too.
    (("SUCCEEDED", 0), True),
    (("FAILED", 1), False),
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


def test_run_flight_passes_config_to_the_named_flight(monkeypatch):
    con = _FakeMotherDuck(statuses=[("SUCCEEDED", 0)])
    monkeypatch.setattr(run_flight.duckdb, "connect", lambda _: con)
    monkeypatch.setattr(run_flight.time, "sleep", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["run_flight.py", "--flight", "initial_load",
                                      "--config", "STEPS=weather", "--config", "DAYS=400"])
    run_flight.main()
    (run_sql,) = [c for c in con.calls if "md_run_flight" in c]
    assert "'STEPS': 'weather'" in run_sql and "'DAYS': '400'" in run_sql
    assert "SOURCES" not in run_sql
    assert any("flight_name = ?" in c for c in con.calls)

    monkeypatch.setattr(sys, "argv", ["run_flight.py", "--flight", "initial_load",
                                      "--config", "NOPE=1"])
    with pytest.raises(SystemExit):
        run_flight.main()


class _QuotaSpent(_FakeMotherDuck):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def execute(self, sql, params=None):
        if "md_run_flight" in sql:
            raise self.error
        return super().execute(sql, params)


def test_run_flight_exits_75_when_the_daily_minutes_are_spent(monkeypatch):
    # The workflows skip (ci.yml) or fall back to the GitHub runner (ingest.yml) on 75.
    spent = run_flight.duckdb.PermissionException(
        "Permission Error: Your organization has used all 30 minutes of Flight run time "
        "included in your plan for today. Wait until tomorrow or upgrade your plan.")
    monkeypatch.setattr(run_flight.duckdb, "connect", lambda _: _QuotaSpent(spent))
    monkeypatch.setattr(sys, "argv", ["run_flight.py", "--sources", "metar"])
    with pytest.raises(SystemExit) as exit_:
        run_flight.main()
    assert exit_.value.code == run_flight.QUOTA_SPENT == 75

    other = run_flight.duckdb.PermissionException("Permission Error: no access to flight")
    monkeypatch.setattr(run_flight.duckdb, "connect", lambda _: _QuotaSpent(other))
    with pytest.raises(run_flight.duckdb.PermissionException):
        run_flight.main()
