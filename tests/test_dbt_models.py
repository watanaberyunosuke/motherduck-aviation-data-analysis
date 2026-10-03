"""Runs the full dbt project against a throwaway DuckDB file.

OpenSky data here is SYNTHETIC: three Sydney -> Melbourne flights built in Python, the
last one with a 12-minute hold inside the Melbourne terminal area and an IFR METAR. The
expected distances and times are computed independently in Python and compared with
what the SQL produces. Weather and NOTAM inputs are real captured responses.
"""
import json
import math
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aviation import warehouse
from aviation.sources import aviationweather, notam_hk

ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
YSSY = (-33.946, 151.177)
YMML = (-37.673, 144.843)
TERMINAL_KM = 92.6

pytestmark = pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")


def haversine(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def synthetic_track(depart: datetime, hold_minutes: int):
    """Straight line SYD -> MEL at one point per minute, optional circular hold ~60 km out."""
    points, t = [], int(depart.timestamp())
    legs = 60
    for i in range(legs + 1):
        f = i / legs
        pos = (YSSY[0] + (YMML[0] - YSSY[0]) * f, YSSY[1] + (YMML[1] - YSSY[1]) * f)
        points.append((t, *pos))
        t += 60
        already_held = any(len(p) == 4 for p in points)
        if hold_minutes and not already_held and 55 <= haversine(pos, YMML) <= 70:
            centre = pos
            for k in range(1, hold_minutes + 1):
                ang = 2 * math.pi * k / 6  # one circuit every 6 minutes
                points.append((t, centre[0] + 0.07 * math.sin(ang), centre[1] + 0.07 * (1 - math.cos(ang)), "hold"))
                t += 60
    path = [[p[0], p[1], p[2], 3000.0, 225.0, False] for p in points]
    return path


def expected_metrics(path):
    coords = [(p[1], p[2]) for p in path]
    path_km = sum(haversine(a, b) for a, b in zip(coords, coords[1:]))
    entry = next(p[0] for p in path if haversine((p[1], p[2]), YMML) <= TERMINAL_KM)
    return path_km, (path[-1][0] - entry) / 60


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    db = tmp_path_factory.mktemp("wh") / "test.duckdb"
    con = warehouse.connect(str(db))

    warehouse.upsert(con, "raw.metar",
                     aviationweather.metar_rows(json.loads((FIXTURES / "metar.json").read_text())),
                     ["icao", "obs_time"])
    warehouse.upsert(con, "raw.taf",
                     aviationweather.taf_rows(json.loads((FIXTURES / "taf.json").read_text())),
                     ["icao", "issue_time"])
    warehouse.upsert(con, "raw.notam",
                     notam_hk.rows(json.loads((FIXTURES / "hk_cad_notams.json").read_text())),
                     ["source", "notam_key"], keep_on_conflict=("first_seen_at",))

    day = datetime(2026, 9, 20, tzinfo=timezone.utc)
    flights, expected = [], {}
    for n, (hour, hold) in enumerate([(1, 0), (3, 0), (5, 12)]):
        icao24 = f"7c00{n:02d}"
        path = synthetic_track(day + timedelta(hours=hour), hold)
        expected[icao24] = expected_metrics(path)
        start, end = path[0][0], path[-1][0]
        flights.append({"icao24": icao24, "first_seen": start - 300, "last_seen": end + 120,
                        "callsign": f"QFA{400 + n}", "est_departure_airport": "YSSY",
                        "est_arrival_airport": "YMML", "fetched_at": warehouse.utcnow(),
                        "payload": {"synthetic": True}})
        warehouse.upsert(con, "raw.opensky_tracks", [{
            "icao24": icao24, "start_time": start, "end_time": end, "callsign": f"QFA{400 + n}",
            "fetched_at": warehouse.utcnow(),
            "payload": {"icao24": icao24, "startTime": start, "endTime": end, "path": path}}],
            ["icao24", "start_time"])
        # A METAR 30 minutes before each arrival; IFR for the held flight.
        obs = end - 1800
        ifr = hold > 0
        warehouse.upsert(con, "raw.metar", [{
            "icao": "YMML", "obs_time": obs, "fetched_at": warehouse.utcnow(),
            "payload": {"icaoId": "YMML", "obsTime": obs, "fltCat": "IFR" if ifr else "VFR",
                        "wspd": 25 if ifr else 8, "wgst": 38 if ifr else None,
                        "visib": 1.5 if ifr else "6+", "wxString": "TSRA" if ifr else None,
                        "clouds": [{"cover": "OVC", "base": 400}] if ifr else [],
                        "rawOb": "SYNTHETIC"}}], ["icao", "obs_time"])
    warehouse.upsert(con, "raw.opensky_flights", flights, ["icao24", "first_seen"])
    con.close()

    env = {**os.environ, "WAREHOUSE": str(db)}
    result = subprocess.run(["dbt", "build", "--profiles-dir", "."], cwd=ROOT / "dbt",
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-3000:]

    import duckdb
    ro = duckdb.connect(str(db), read_only=True)
    yield ro, expected
    ro.close()


def test_track_metrics_match_independent_python(built):
    con, expected = built
    rows = con.execute("""select icao24, path_km, terminal_minutes, great_circle_km,
                                 route_inefficiency, has_full_coverage
                          from marts.fct_flight_track_metrics order by icao24""").fetchall()
    assert len(rows) == 3
    for icao24, path_km, terminal_min, gc_km, ineff, full in rows:
        exp_path, exp_terminal = expected[icao24]
        assert path_km == pytest.approx(exp_path, rel=1e-6)
        assert terminal_min == pytest.approx(exp_terminal, abs=1e-6)
        assert gc_km == pytest.approx(haversine(YSSY, YMML), rel=1e-6)
        assert full
    held = dict((r[0], r[4]) for r in rows)["7c0002"]
    straight = dict((r[0], r[4]) for r in rows)["7c0000"]
    assert held > straight, "a holding pattern must increase route inefficiency"


def test_arrival_impact_flags_hold_against_baseline(built):
    con, expected = built
    rows = con.execute("""select icao24, excess_terminal_minutes, baseline_flights, is_ifr,
                                 has_thunderstorm, has_current_metar, wind_gust_kt
                          from marts.fct_arrival_weather_impact order by arrived_at""").fetchall()
    assert [r[0] for r in rows] == ["7c0000", "7c0001", "7c0002"]
    first, second, held = rows
    assert first[2] == 0 and first[1] is None, "first flight has no baseline yet"
    exp_excess = expected["7c0002"][1] - expected["7c0000"][1]
    assert held[1] == pytest.approx(exp_excess, abs=1e-6)
    # The fixture inserts exactly one 12-minute hold, one point per minute.
    assert held[1] == pytest.approx(12, abs=1)
    assert held[3] and held[4] and held[5] and held[6] == 38
    assert not second[3]


def test_notam_flags_unknown_without_notam_feed(built):
    con, _ = built
    rows = con.execute("""select has_notam_feed, surface_notam_in_force, runway_closure_in_force
                          from marts.fct_arrival_weather_impact""").fetchall()
    # YMML has no NOTAM source, so the flags must be unknown rather than "none in force".
    assert rows and all(r == (False, None, None) for r in rows)


def test_weather_hourly_from_real_metars(built):
    con, _ = built
    n, cats = con.execute("""select count(*), list(distinct flight_category)
                             from marts.fct_airport_weather_hourly""").fetchone()
    assert n > 0 and set(cats) <= {"VFR", "MVFR", "IFR", "LIFR", None}
