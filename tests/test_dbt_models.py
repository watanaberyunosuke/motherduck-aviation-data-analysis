"""Runs the full dbt project against a throwaway DuckDB file.

OpenSky data here is SYNTHETIC: three Sydney -> Melbourne flights built in Python, the
last one with a 12-minute hold inside the Melbourne terminal area and an IFR METAR, then
an Auckland -> Melbourne arrival whose track is only seen near Melbourne, and an untracked
light aircraft. Brisbane gets synthetic AeroDataBox boards for two days (a flown flight
and a cancellation each): on the first OpenSky has the same flight, so AeroDataBox's copy
must give way; on the second OpenSky came back empty, so AeroDataBox's flight must stay. The expected distances and times are computed independently in Python
and compared with what the SQL produces. Weather and NOTAM inputs are real captured
responses.
"""
import json
import math
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aviation import warehouse
from aviation.sources import aerodatabox, aviationweather, notam_hk

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


def approach_track(start: datetime, from_pos, minutes: int):
    """Straight line from `from_pos` to YMML, one point per minute: the arrival end only."""
    t = int(start.timestamp())
    return [[t + 60 * i,
             from_pos[0] + (YMML[0] - from_pos[0]) * i / minutes,
             from_pos[1] + (YMML[1] - from_pos[1]) * i / minutes, 3000.0, 250.0, False]
            for i in range(minutes + 1)]


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

    # Auckland -> Melbourne, first seen ~300 km east of Melbourne (no departure coverage).
    path = approach_track(day + timedelta(hours=8), (YMML[0], YMML[1] + 3.4), 40)
    expected["7c0003"] = expected_metrics(path)
    start, end = path[0][0], path[-1][0]
    flights.append({"icao24": "7c0003", "first_seen": start - 3 * 3600, "last_seen": end + 120,
                    "callsign": "ANZ0123 ", "est_departure_airport": "NZAA",
                    "est_arrival_airport": "YMML", "fetched_at": warehouse.utcnow(),
                    "payload": {"synthetic": True}})
    warehouse.upsert(con, "raw.opensky_tracks", [{
        "icao24": "7c0003", "start_time": start, "end_time": end, "callsign": "ANZ0123",
        "fetched_at": warehouse.utcnow(),
        "payload": {"icao24": "7c0003", "startTime": start, "endTime": end, "path": path}}],
        ["icao24", "start_time"])
    # An alphanumeric ATC callsign: not a flight number, so it must not map.
    flights.append({"icao24": "7c0005", "first_seen": start + 600, "last_seen": end + 900,
                    "callsign": "QLK10D", "est_departure_airport": "YSSY",
                    "est_arrival_airport": "YMML", "fetched_at": warehouse.utcnow(),
                    "payload": {"synthetic": True}})
    # A light aircraft with a registration callsign and no track.
    t = int((day + timedelta(hours=10)).timestamp())
    flights.append({"icao24": "7c0004", "first_seen": t, "last_seen": t + 3600,
                    "callsign": "VHABC", "est_departure_airport": "YMMB",
                    "est_arrival_airport": "YMML", "fetched_at": warehouse.utcnow(),
                    "payload": {"synthetic": True}})
    # Brisbane: an AeroDataBox board each for 21 and 22 Sep, one flown flight (12 min late)
    # and one cancellation each.
    for d, number, mode_s in ((21, 145, "C81234"), (22, 147, "C80999")):
        board = {"arrivals": [
            {"departure": {"airport": {"icao": "NZAA", "iata": "AKL"},
                           "scheduledTime": {"utc": f"2026-09-{d} 01:00Z"},
                           "runwayTime": {"utc": f"2026-09-{d} 01:15Z"}},
             "arrival": {"scheduledTime": {"utc": f"2026-09-{d} 04:00Z"},
                         "revisedTime": {"utc": f"2026-09-{d} 04:10Z"},
                         "runwayTime": {"utc": f"2026-09-{d} 04:12Z"}, "runway": "19R"},
             "number": f"NZ {number}", "status": "Arrived",
             "aircraft": {"reg": "ZK-NZA", "modeS": mode_s},
             "airline": {"name": "Air New Zealand", "iata": "NZ", "icao": "ANZ"}},
            {"departure": {"airport": {"icao": "NZAA"}, "scheduledTime": {"utc": f"2026-09-{d} 03:00Z"}},
             "arrival": {"scheduledTime": {"utc": f"2026-09-{d} 06:00Z"}},
             "number": "JQ 200", "status": "Canceled", "airline": {"icao": "JST"}},
        ], "departures": []}
        warehouse.upsert(con, "raw.aerodatabox_flights", aerodatabox.flight_rows("YBBN", board),
                         ["airport_icao", "direction", "flight_id"])
        for direction in ("arrival", "departure"):
            warehouse.mark_slot(con, "aerodatabox", "YBBN", direction, datetime(2026, 9, d).date(), 1)
    # 21 Sep: OpenSky has the slot with the same flight, so its copy replaces AeroDataBox's.
    # 22 Sep: OpenSky loaded the slot but saw nothing, so AeroDataBox's flight stays.
    adb_day = int(datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp())
    flights.append({"icao24": "c81234", "first_seen": adb_day + 4800, "last_seen": adb_day + 15060,
                    "callsign": "ANZ145", "est_departure_airport": "NZAA",
                    "est_arrival_airport": "YBBN", "fetched_at": warehouse.utcnow(),
                    "payload": {"synthetic": True}})
    warehouse.mark_slot(con, "opensky", "YBBN", "arrival", datetime(2026, 9, 21).date(), 1)
    warehouse.mark_slot(con, "opensky", "YBBN", "arrival", datetime(2026, 9, 22).date(), 0)
    warehouse.upsert(con, "raw.opensky_flights", flights, ["icao24", "first_seen"])

    # Outside-METAR weather. YMML's METARs are old, so the best source per field wins: the
    # national service for temperature, Open-Meteo for dew point and text. YSSY gets a
    # fresh METAR with no weather group: it stays "Nil" and keeps its own temperature.
    now = warehouse.utcnow()

    def extra(icao, source, temp, dew, text):
        return {"icao": icao, "source": source, "observed_at": now, "fetched_at": now,
                "temp_c": temp, "dewpoint_c": dew, "wx_text": text, "payload": {"synthetic": True}}

    warehouse.upsert(con, "raw.wx_extra", [
        extra("YMML", "gov", 20.0, None, None),
        extra("YMML", "open-meteo", 21.0, 12.0, "Light rain"),
        extra("YMML", "met.no", 22.0, 13.0, "Rain"),
        extra("YSSY", "open-meteo", 30.0, 20.0, "Light rain"),
    ], ["icao", "source", "observed_at"])
    warehouse.upsert(con, "raw.metar", [{
        "icao": "YSSY", "obs_time": int(now.timestamp()) - 600, "fetched_at": now,
        "payload": {"icaoId": "YSSY", "obsTime": int(now.timestamp()) - 600, "temp": 15.0,
                    "dewp": 9.0, "wxString": None, "rawOb": "SYNTHETIC FRESH"}}],
        ["icao", "obs_time"])
    warehouse.upsert(con, "raw.procedure_links", [{
        "icao": "PANC", "name": "FAA d-TPP", "url": "https://example.test/panc", "kind": "cycle",
        "checked_at": now}], ["icao"])
    warehouse.upsert(con, "raw.atis", [{
        "icao": "VHHH", "text_hash": "h1", "arrival_letter": "B", "departure_letter": "C",
        "text": "SYNTHETIC ATIS",
        "first_seen_at": now, "last_seen_at": now}], ["icao", "text_hash"])
    warehouse.upsert(con, "raw.sun_times", [{
        "icao": "YMML", "day": now.astimezone(ZoneInfo("Australia/Melbourne")).date(),
        "source": "computed", "sunrise": now.replace(hour=20, minute=5, second=0, microsecond=0),
        "sunset": now.replace(hour=8, minute=30, second=0, microsecond=0), "fetched_at": now}],
        ["icao", "day"])

    # Hong Kong gate times and OpenSky flights for taxi times, 21 Sep (UTC). CX870 pushes
    # back at 10:00 and OpenSky first sees CPA0870 (leading zero) at 10:19: taxi-out 19.
    # CX871 is last seen at 12:00 and on its stand at 12:07: taxi-in 7. CX872's only
    # OpenSky flight is 2 h after pushback, too late to be its take-off. KA9's board entry
    # has no gate time (cancelled).
    hk = datetime(2026, 9, 21, tzinfo=timezone.utc)

    def board(direction, flight, callsign, gate_h, gate_m, status):
        sched = hk + timedelta(hours=gate_h)
        gate = None if gate_m is None else hk + timedelta(hours=gate_h, minutes=gate_m)
        return {"direction": direction, "flight": flight, "scheduled_at": sched, "callsign": callsign,
                "is_cargo": False, "status": status, "gate_at": gate, "board_date": sched.date(),
                "loaded_for": sched.date(), "fetched_at": now, "payload": {"synthetic": True}}

    warehouse.upsert(con, "raw.hkia_flights", [
        board("departure", "CX870", "CPA870", 10, 0, "Dep"),
        board("arrival", "CX871", "CPA871", 12, 7, "At gate"),
        board("departure", "CX872", "CPA872", 14, 0, "Dep"),
        board("departure", "KA9", "HDA9", 15, None, "Cancelled"),
    ], ["direction", "flight", "scheduled_at"])

    def opensky(icao24, callsign, first_h, first_m, minutes, dep, arr):
        first = int((hk + timedelta(hours=first_h, minutes=first_m)).timestamp())
        return {"icao24": icao24, "first_seen": first, "last_seen": first + minutes * 60, "callsign": callsign,
                "est_departure_airport": dep, "est_arrival_airport": arr, "fetched_at": now,
                "payload": {"synthetic": True}}

    warehouse.upsert(con, "raw.opensky_flights", [
        opensky("780870", "CPA0870 ", 10, 19, 240, "VHHH", "RJAA"),
        opensky("780871", "CPA871", 8, 0, 240, "RJAA", "VHHH"),
        opensky("780872", "CPA872", 16, 0, 240, "VHHH", "RJAA"),
    ], ["icao24", "first_seen"])
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
                                 route_inefficiency, has_full_coverage, has_arrival_coverage
                          from marts.fct_flight_track_metrics order by icao24""").fetchall()
    assert len(rows) == 4
    for icao24, path_km, terminal_min, gc_km, ineff, full, arrival in rows:
        exp_path, exp_terminal = expected[icao24]
        assert path_km == pytest.approx(exp_path, rel=1e-6)
        assert terminal_min == pytest.approx(exp_terminal, abs=1e-6)
        assert arrival
        if icao24 == "7c0003":
            # Auckland is in airport_codes, so the great circle is known, but the track
            # starts 300 km out: no departure coverage.
            assert gc_km == pytest.approx(2600, rel=0.05) and not full
        else:
            assert gc_km == pytest.approx(haversine(YSSY, YMML), rel=1e-3)
            assert full
    held = dict((r[0], r[4]) for r in rows)["7c0002"]
    straight = dict((r[0], r[4]) for r in rows)["7c0000"]
    assert held > straight, "a holding pattern must increase route inefficiency"


def test_arrival_impact_flags_hold_against_baseline(built):
    con, expected = built
    rows = con.execute("""select icao24, excess_terminal_minutes, baseline_flights, is_ifr,
                                 has_thunderstorm, has_current_metar, wind_gust_kt
                          from marts.fct_arrival_weather_impact order by arrived_at""").fetchall()
    assert [r[0] for r in rows] == ["7c0000", "7c0001", "7c0002", "7c0003"]
    first, second, held, _ = rows
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


def test_iata_codes_stored_alongside_icao(built):
    con, _ = built
    rows = con.execute("""select icao24, callsign, flight_number_iata, departure_icao,
                                 departure_iata, arrival_icao, arrival_iata
                          from marts.fct_arrivals order by icao24""").fetchall()
    by_id = {r[0]: r[1:] for r in rows}
    assert by_id["7c0000"] == ("QFA400", "QF400", "YSSY", "SYD", "YMML", "MEL")
    assert by_id["7c0003"] == ("ANZ0123", "NZ123", "NZAA", "AKL", "YMML", "MEL")
    # Registrations and alphanumeric ATC callsigns are not airline flight numbers.
    assert by_id["7c0004"][:2] == ("VHABC", None)
    assert by_id["7c0005"][:2] == ("QLK10D", None)

    impact = con.execute("""select icao24, flight_number_iata, arrival_iata
                            from marts.fct_arrival_weather_impact order by icao24""").fetchall()
    assert impact[0] == ("7c0000", "QF400", "MEL")
    hourly = con.execute("""select count(*), count(iata) from marts.fct_airport_weather_hourly""").fetchone()
    assert hourly[0] == hourly[1] > 0


def test_arrivals_include_every_origin(built):
    con, expected = built
    rows = con.execute("""select icao24, terminal_minutes from marts.fct_arrivals
                          where arrival_icao = 'YMML' order by arrived_at""").fetchall()
    assert [r[0] for r in rows] == ["7c0000", "7c0001", "7c0002", "7c0003", "7c0005", "7c0004"]
    terminal = dict(rows)
    assert terminal["7c0003"] == pytest.approx(expected["7c0003"][1], abs=1e-6)
    assert terminal["7c0004"] is None, "untracked arrivals are listed without terminal metrics"


def test_departures_measure_time_to_leave_terminal_area(built):
    con, _ = built
    rows = con.execute("""select icao24, departure_iata, arrival_iata, departure_terminal_minutes,
                                 excess_departure_minutes
                          from marts.fct_departures where departure_icao != 'VHHH'
                          order by departed_at""").fetchall()
    # The three synthetic SYD -> MEL flights and the QLK10D flight depart an in-scope
    # airport; the Auckland and YMMB departures do not. (Hong Kong's are for taxi times.)
    assert [r[0] for r in rows] == ["7c0000", "7c0001", "7c0002", "7c0005"]
    for icao24, dep, arr, minutes, _ in rows[:3]:
        assert (dep, arr) == ("SYD", "MEL")
        path = synthetic_track(datetime(2026, 9, 20, tzinfo=timezone.utc)
                               + timedelta(hours={"7c0000": 1, "7c0001": 3, "7c0002": 5}[icao24]),
                               12 if icao24 == "7c0002" else 0)
        exit_t = next(p[0] for p in path if haversine((p[1], p[2]), YSSY) > TERMINAL_KM)
        assert minutes == pytest.approx((exit_t - path[0][0]) / 60, abs=1e-6)
    assert rows[0][4] is None, "first departure has no baseline yet"
    assert rows[3][3] is None, "untracked departures have no terminal metrics"


def test_taxi_times_pair_hong_kong_gate_times_with_opensky(built):
    con, _ = built
    rows = con.execute("""select direction, flight, airport_icao, taxi_minutes,
                                 strftime(runway_at at time zone 'UTC', '%H:%M')
                          from marts.fct_taxi_times order by direction desc, flight""").fetchall()
    assert rows == [
        ("departure", "CX870", "VHHH", 19.0, "10:19"),
        ("arrival", "CX871", "VHHH", 7.0, "12:00"),
    ]


def test_airport_conditions_one_row_per_airport(built):
    con, _ = built
    rows = con.execute("""select icao, iata, timezone, metar_raw is not null, notams_in_force
                          from marts.fct_airport_conditions order by icao""").fetchall()
    assert [r[0] for r in rows] == ["EHAM", "PANC", "VHHH", "WSSS", "YBBN", "YMML", "YSSY"]
    by_icao = {r[0]: r for r in rows}
    assert by_icao["YMML"][1:4] == ("MEL", "Australia/Melbourne", True)
    assert by_icao["YMML"][4] is None, "no NOTAM feed: unknown, not zero"
    assert by_icao["VHHH"][4] is not None
    # EHAM has a NOTAM source (faa), but it never loaded here, so still unknown.
    assert by_icao["EHAM"][1:3] == ("AMS", "Europe/Amsterdam") and by_icao["EHAM"][4] is None


def test_conditions_fall_back_to_outside_weather_only_when_metar_is_stale(built):
    con, _ = built
    rows = {r[0]: r for r in con.execute("""
        select icao, temp_c, temp_source, dewpoint_c, dewpoint_source, wx_text, wx_text_source,
               sunrise_local, sun_source
        from marts.fct_airport_conditions where icao in ('YMML', 'YSSY', 'EHAM')""").fetchall()}
    # Stale METAR: gov for temperature, Open-Meteo for the fields gov lacks.
    assert rows["YMML"][1:7] == (20.0, "gov", 12.0, "open-meteo", "Light rain", "open-meteo")
    assert rows["YMML"][8] == "computed" and rows["YMML"][7] is not None
    # Fresh METAR: its own values, and no model text over "Nil".
    assert rows["YSSY"][1:7] == (15.0, "metar", 9.0, "metar", None, None)
    # Nothing outside for EHAM: no source is claimed unless the METAR really had a value.
    assert rows["EHAM"][2] in (None, "metar") and rows["EHAM"][5] is None


def test_procedure_link_reaches_the_conditions_mart(built):
    con, _ = built
    rows = dict(con.execute("""select icao, procedures_url from marts.fct_airport_conditions
                               where procedures_url is not null""").fetchall())
    assert rows == {"PANC": "https://example.test/panc"}


def test_atis_only_for_the_airport_that_has_one(built):
    con, _ = built
    rows = dict(con.execute("""select icao, atis_arrival_letter || atis_departure_letter || ': ' || atis_text
                               from marts.fct_airport_conditions where atis_text is not null""").fetchall())
    assert rows == {"VHHH": "BC: SYNTHETIC ATIS"}


def test_terminal_tracks_stay_near_the_airport(built):
    con, _ = built
    rows = con.execute("""select airport_icao, role, count(distinct icao24), max(
                                 2 * 6371.0088 * asin(sqrt(pow(sin(radians(t.lat - a.lat) / 2), 2)
                                 + cos(radians(a.lat)) * cos(radians(t.lat))
                                 * pow(sin(radians(t.lon - a.lon) / 2), 2))))
                          from marts.fct_terminal_tracks t
                          join reference.airports a on a.icao = t.airport_icao
                          group by 1, 2 order by 1, 2""").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("YMML", "arrival", 4), ("YSSY", "departure", 3)]
    assert all(r[3] <= 250 for r in rows)


def test_opensky_preferred_and_aerodatabox_fills_its_gaps_with_schedule_delay(built):
    con, _ = built
    rows = con.execute("""select source, icao24, callsign, flight_number_iata, departure_iata,
                                 strftime(arrived_at at time zone 'UTC', '%Y-%m-%d %H:%M'), delay_minutes
                          from marts.fct_arrivals where arrival_icao = 'YBBN'
                          order by arrived_at""").fetchall()
    # 21 Sep: OpenSky's NZ145 (no schedule); AeroDataBox's copy is gone. 22 Sep: OpenSky
    # saw nothing, so AeroDataBox's NZ147 (runway time, 12 min late). Both cancelled JQ200s
    # are gone.
    assert rows == [
        ("opensky", "c81234", "ANZ145", "NZ145", "AKL", "2026-09-21 04:11", None),
        ("aerodatabox", "c80999", "ANZ147", "NZ147", "AKL", "2026-09-22 04:12", 12.0),
    ]
    moves = con.execute("""select day_utc::varchar, arrivals from marts.fct_daily_airport_movements
                           where icao = 'YBBN' order by day_utc""").fetchall()
    assert moves == [("2026-09-21", 1), ("2026-09-22", 1)]


def test_freighters_tagged_by_cargo_operator_as_the_api_does(built, tmp_path):
    con, _ = built
    # The synthetic flights are all passenger callsigns, registrations or ATC callsigns.
    for mart in ("fct_arrivals", "fct_departures"):
        assert con.execute(f"select count(*) filter (where is_freighter), count(is_freighter), count(*) "
                           f"from marts.{mart}").fetchone()[0] == 0

    # The macro against sample callsigns, compiled by dbt and run on the built warehouse.
    # dbt compiles against an empty file of the same name (the built one is open here), so
    # the seed's relation names the same catalog.
    samples = ["FDX5150", "CLX7", "UPS12AB", "CPA101", "QFA627", "VHABC", "FDX", "fdx5150", None]
    values = ", ".join("(null)" if c is None else f"('{c}')" for c in samples)
    db = Path(con.execute("select path from duckdb_databases() "
                          "where database_name = current_database()").fetchone()[0])
    compiled = subprocess.run(
        ["dbt", "--quiet", "compile", "--profiles-dir", ".", "--inline",
         f"select c, {{{{ is_freighter('c') }}}} as f from (values {values}) t(c)"],
        cwd=ROOT / "dbt", env={**os.environ, "WAREHOUSE": str(tmp_path / db.name)},
        capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stdout[-3000:]
    tagged = dict(con.execute(compiled.stdout).fetchall())
    assert {c for c, f in tagged.items() if f} == {"FDX5150", "CLX7", "UPS12AB"}

    import importlib.util
    spec = importlib.util.spec_from_file_location("api_index", ROOT / "api" / "index.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    operators = frozenset(r[0] for r in con.execute("select icao from reference.cargo_operators").fetchall())
    assert {c: api.is_freighter(c, operators) for c in samples} == tagged
