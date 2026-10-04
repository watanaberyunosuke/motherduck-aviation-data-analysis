import json
from pathlib import Path

import pytest

from aviation import warehouse
from aviation.sources import (
    aerodatabox, aviationweather, iem, notam_faa, notam_faa_search, notam_hk, notam_rapidapi,
    opensky,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def con(tmp_path):
    c = warehouse.connect(str(tmp_path / "t.duckdb"))
    yield c
    c.close()


def test_metar_load_is_idempotent(con):
    payload = json.loads((FIXTURES / "metar.json").read_text())
    rows = aviationweather.metar_rows(payload)
    warehouse.upsert(con, "raw.metar", rows, ["icao", "obs_time"])
    warehouse.upsert(con, "raw.metar", aviationweather.metar_rows(payload), ["icao", "obs_time"])
    assert con.execute("select count(*) from raw.metar").fetchone()[0] == len(payload)


def test_hk_notams_keep_first_seen_on_reload(con):
    payload = json.loads((FIXTURES / "hk_cad_notams.json").read_text())
    first = notam_hk.rows(payload)
    warehouse.upsert(con, "raw.notam", first, ["source", "notam_key"], keep_on_conflict=("first_seen_at",))
    second = notam_hk.rows(payload)
    warehouse.upsert(con, "raw.notam", second, ["source", "notam_key"], keep_on_conflict=("first_seen_at",))

    n, stale = con.execute("""
        select count(*), count(*) filter (where first_seen_at = last_seen_at)
        from raw.notam""").fetchone()
    assert n == len(payload["notam"])
    assert stale == 0, "second load should advance last_seen_at but keep first_seen_at"


# Shape copied from the provider's documented example (github.com/SkyLink-API/notam-api).
# The ICAO raw text is an assumption for non-US airports; see notam_rapidapi docstring.
RAPIDAPI_SAMPLE = {
    "icao": "YSSY",
    "total_count": 2,
    "notams": [
        {"notam_id": "C1234/26", "type": "N", "location": "YSSY",
         "effective": "2026-10-01T12:00:00Z", "expiration": "2026-10-02T06:00:00Z",
         "body": "RWY 16R/34L CLSD",
         "raw": "(C1234/26 NOTAMN\nQ) YMMM/QMRLC/IV/NBO/A /000/999/3357S15111E005\n"
                "A) YSSY B) 2610011200 C) 2610020600\nE) RWY 16R/34L CLSD)",
         "source": "FAA SWIM FNS"},
        {"notam_id": "1/0001", "type": "N", "location": "YSSY",
         "effective": "2026-10-01T00:00:00Z", "expiration": "PERM",
         "body": "OBST CRANE", "raw": "!SYD 10/001 SYD OBST CRANE", "source": "FAA SWIM FNS"},
    ],
}


def test_rapidapi_uses_structured_fields_and_fills_q_code_when_icao():
    rows = notam_rapidapi.rows("YSSY", RAPIDAPI_SAMPLE)
    icao_fmt, domestic = rows
    assert icao_fmt["q_code"] == "QMRLC" and icao_fmt["notam_key"] == "YSSY:C1234/26"
    assert icao_fmt["starts_at"].isoformat() == "2026-10-01T12:00:00+00:00"
    assert domestic["q_code"] is None and domestic["is_permanent"] is True
    assert domestic["body"] == "OBST CRANE"


def test_rapidapi_schema_change_fails_loudly():
    with pytest.raises(notam_rapidapi.SchemaMismatch):
        notam_rapidapi.rows("YSSY", {"data": []})


# Shape from the FAA NOTAM API docs (GeoJSON format, trimmed). One international NOTAM with
# an ICAO translation, one US domestic NOTAM with only the local-format text.
def _faa_item(notam: dict, translations: list[dict]) -> dict:
    return {"type": "Feature", "geometry": None,
            "properties": {"coreNOTAMData": {"notam": notam, "notamTranslation": translations}}}


FAA_ITEMS = [
    _faa_item(
        {"id": "NOTAM_1", "number": "A1234/26", "type": "N", "selectionCode": "QMRLC",
         "icaoLocation": "EHAM", "effectiveStart": "2026-10-01T22:00:00.000Z",
         "effectiveEnd": "2026-10-02T05:00:00.000Z", "text": "RWY 18R/36L CLSD",
         "classification": "INTL"},
        [{"type": "ICAO", "formattedText":
          "A1234/26 NOTAMN\nQ) EHAA/QMRLC/IV/NBO/A/000/999/5219N00446E005\n"
          "A) EHAM B) 2610012200 C) 2610020500 EST\nE) RWY 18R/36L CLSD"}]),
    _faa_item(
        {"id": "NOTAM_2", "number": "10/045", "type": "N", "icaoLocation": "PANC",
         "effectiveStart": "2026-10-01T00:00:00.000Z", "effectiveEnd": "PERM",
         "text": "OBST CRANE 610218N1495937W 210FT AGL", "classification": "DOM"},
        [{"type": "LOCAL_FORMAT", "simpleText": "!ANC 10/045 ANC OBST CRANE"}]),
]


def test_faa_parses_icao_translation_and_falls_back_to_structured_fields():
    now = warehouse.utcnow()
    intl, dom = notam_faa.rows("EHAM", FAA_ITEMS, now)
    assert intl["notam_key"] == "EHAM:A1234/26" and intl["fir"] == "EHAA"
    assert intl["q_code"] == "QMRLC" and intl["is_estimated"] is True
    assert intl["ends_at"].isoformat() == "2026-10-02T05:00:00+00:00"
    assert intl["raw_text"].startswith("A1234/26 NOTAMN")
    assert dom["notam_key"] == "PANC:10/045" and dom["location"] == "PANC"
    assert dom["q_code"] is None and dom["is_permanent"] is True and dom["ends_at"] is None
    assert dom["starts_at"].isoformat() == "2026-10-01T00:00:00+00:00"
    assert dom["raw_text"] == dom["body"] == "OBST CRANE 610218N1495937W 210FT AGL"
    assert intl["last_seen_at"] == dom["last_seen_at"] == now


def test_faa_rows_load_into_raw_notam(con):
    rows = notam_faa.rows("EHAM", FAA_ITEMS, warehouse.utcnow())
    warehouse.upsert(con, "raw.notam", rows, ["source", "notam_key"], keep_on_conflict=("first_seen_at",))
    assert con.execute("select count(*) from raw.notam where source = 'faa'").fetchone()[0] == 2


def test_faa_schema_change_fails_loudly():
    with pytest.raises(notam_faa.SchemaMismatch):
        notam_faa.rows("EHAM", [{"properties": {}}], warehouse.utcnow())


# Real records from the FAA NOTAM Search backend, one of each kind (see the fixture's _note).
FAA_SEARCH = json.loads((FIXTURES / "faa_notam_search.json").read_text())


def test_faa_search_drops_military_and_letters_to_airmen():
    rows = notam_faa_search.rows("EHAM", FAA_SEARCH["notamList"], warehouse.utcnow())
    keys = [r["notam_key"] for r in rows]
    assert keys == ["EHAM:A1954/26", "EHAM:A1731/26", "PANC:10/028", "PANC:5/2149", "PANC:09/183"]


def test_faa_search_parses_icao_text_and_falls_back_for_domestic():
    by_key = {r["notam_key"]: r for r in
              notam_faa_search.rows("EHAM", FAA_SEARCH["notamList"], warehouse.utcnow())}
    sched = by_key["EHAM:A1954/26"]
    assert sched["q_code"] == "QPOCH" and sched["fir"] == "EHAA" and sched["schedule"] == "MON-FRI 0500-1500"
    assert by_key["EHAM:A1731/26"]["is_estimated"] is True
    assert by_key["EHAM:A1731/26"]["replaces"] == "A1495/26"
    # FAA rendering of a US NOTAM: domestic number, 3-letter FIR.
    assert by_key["PANC:10/028"]["fir"] == "ZAN" and by_key["PANC:10/028"]["q_code"] == "QMXXX"
    assert by_key["PANC:5/2149"]["q_code"] == "QPDCH"
    # US domestic format: no Q-line, dates from the record's own fields.
    dom = by_key["PANC:09/183"]
    assert dom["q_code"] is None and dom["raw_text"].startswith("!ANC 09/183")
    assert dom["starts_at"].isoformat() == "2026-09-20T07:45:00+00:00"
    assert dom["ends_at"].isoformat() == "2026-10-20T07:45:00+00:00"


def test_faa_search_cleans_html_entities():
    assert notam_faa_search._clean(" 1500M&#8203; 369\u200b ") == "1500M 369"
    assert notam_faa_search._date("12/31/2026 2300EST").isoformat() == "2026-12-31T23:00:00+00:00"
    assert notam_faa_search._date("PERM") is None


class _FakeSearchSession:
    """Answers the search POST page by page, like the site (30 per page there)."""

    def __init__(self, records, page_size=3, content_type="application/json;charset=UTF-8"):
        self.records, self.page_size, self.content_type = records, page_size, content_type
        self.offsets = []

    def post(self, url, timeout, headers, data):
        offset = int(data["offset"])
        self.offsets.append(offset)
        page = self.records[offset:offset + self.page_size]
        body = {"notamList": page, "totalNotamCount": len(self.records), "error": ""}
        return _FakeSearchResponse(body, self.content_type)


class _FakeSearchResponse:
    def __init__(self, body, content_type):
        self.status_code, self._body = 200, body
        self.headers = {"content-type": content_type}

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def test_faa_search_fetches_every_page(monkeypatch):
    monkeypatch.setattr(notam_faa_search, "PAUSE_SECONDS", 0)
    session = _FakeSearchSession(FAA_SEARCH["notamList"])
    items = notam_faa_search.fetch(session, "EHAM")
    assert len(items) == len(FAA_SEARCH["notamList"]) and session.offsets == [0, 3, 6]


def test_faa_search_reports_akamai_block(monkeypatch):
    session = _FakeSearchSession([], content_type="text/html")
    with pytest.raises(notam_faa_search.Blocked):
        notam_faa_search.fetch(session, "EHAM")


def test_faa_search_rows_load_into_raw_notam(con):
    rows = notam_faa_search.rows("EHAM", FAA_SEARCH["notamList"], warehouse.utcnow())
    warehouse.upsert(con, "raw.notam", rows, ["source", "notam_key"], keep_on_conflict=("first_seen_at",))
    assert con.execute("select count(*) from raw.notam where source = 'faa_search'").fetchone()[0] == 5


def test_opensky_day_window_is_a_whole_utc_day():
    from datetime import datetime, timezone
    begin, end = opensky.day_window(1, now=datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc))
    assert datetime.fromtimestamp(begin, timezone.utc) == datetime(2026, 9, 26, tzinfo=timezone.utc)
    assert end - begin == 86400


def test_opensky_tracks_newest_flights_first(con):
    in_scope = {"est_departure_airport": "YSSY", "est_arrival_airport": "YMML"}
    flights = [
        {"icao24": "old", "first_seen": 1000, "last_seen": 5000, **in_scope},
        {"icao24": "new", "first_seen": 3000, "last_seen": 9000, **in_scope},
        {"icao24": "mid", "first_seen": 2000, "last_seen": 7000, **in_scope},
        {"icao24": "out", "first_seen": 2000, "last_seen": 8000,
         "est_departure_airport": "YSSY", "est_arrival_airport": "NZAA"},
    ]
    rows = [{**f, "callsign": None, "fetched_at": warehouse.utcnow(), "payload": {}} for f in flights]
    warehouse.upsert(con, "raw.opensky_flights", rows, ["icao24", "first_seen"])

    todo = opensky.select_flights_to_track(con, ["YSSY", "YMML"], 0, 10_000, True, limit=2)
    assert [t[0] for t in todo] == ["new", "mid"]


class _FakeResponse:
    def __init__(self, status, body=None, remaining=None):
        self.status_code = status
        self._body = body
        self.headers = {} if remaining is None else {"X-Rate-Limit-Remaining": str(remaining)}

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def test_opensky_run_log_counts_http_statuses(con, monkeypatch):
    from datetime import datetime, timezone
    # After 06 UTC, so yesterday is the newest complete day.
    monkeypatch.setattr(warehouse, "utcnow", lambda: datetime(2026, 10, 5, 7, tzinfo=timezone.utc))
    begin, end = opensky.day_window(1)
    flights = [{"icao24": f"a{i}", "firstSeen": begin + i * 100, "lastSeen": begin + i * 100 + 50,
                "estDepartureAirport": "YSSY", "estArrivalAirport": "YMML"} for i in range(3)]
    track = {"icao24": "a2", "startTime": float(begin + 200), "endTime": begin + 250,
             "path": [[begin + 200, -33.9, 151.2, 0, 0, False]]}
    responses = iter([
        _FakeResponse(200, flights, remaining=3000),  # YSSY arrivals
        _FakeResponse(404),                           # YSSY departures
        _FakeResponse(404),                           # YMML arrivals
        _FakeResponse(404),                           # YMML departures
        _FakeResponse(200, track, remaining=970),     # a2 (newest)
        _FakeResponse(404),                           # a1
        _FakeResponse(429),                           # a0
    ])
    monkeypatch.setattr(opensky.OpenSkyClient, "_auth_header", lambda self: {})
    monkeypatch.setattr(opensky, "session", lambda: type("S", (), {"get": lambda *a, **k: next(responses)})())

    stats = opensky.ingest(con, ["YSSY", "YMML"], {"days_back": 1, "min_credits_remaining": 0})

    assert stats["tracks"] == 1 and "429" in stats["stopped_early"]
    detail = con.execute("select detail from raw.ingest_log where source = 'opensky'").fetchone()[0]
    assert "http={'flights': {200: 1, 404: 3}, 'tracks': {200: 1, 404: 1, 429: 1}}" in detail


# IEM rows as its CSV service returns them (data=metar plus decoded columns).
IEM_EHAM = {"station": "EHAM", "valid": "2023-10-05 00:25", "tmpf": "62.60", "dwpf": "51.80",
            "drct": "260.00", "sknt": "17.00", "gust": "", "vsby": "6.21", "alti": "30.15",
            "wxcodes": "", "skyc1": "FEW", "skyl1": "2500.00",
            "metar": "EHAM 050025Z 26017KT 9999 FEW025 17/11 Q1021 NOSIG"}
IEM_FOG = {"station": "YSSY", "valid": "2023-10-06 19:00", "drct": "", "sknt": "3.00",
           "vsby": "0.25", "alti": "29.97", "wxcodes": "FG", "skyc1": "VV", "skyl1": "100.00",
           "metar": "YSSY 061900Z VRB03KT 0400 FG VV001 12/12 Q1015"}


def test_iem_rows_take_the_awc_payload_shape():
    p = iem.to_awc_payload(IEM_EHAM, "METAR")
    assert p["obsTime"] == 1696465500 and p["rawOb"].startswith("METAR EHAM 050025Z")
    assert (p["temp"], p["dewp"], p["altim"], p["wdir"], p["wspd"]) == (17, 11, 1021, 260, 17)
    assert p["visib"] == "6+" and p["clouds"] == [{"cover": "FEW", "base": 2500}]
    assert p["fltCat"] == "VFR" and p["source"] == "iem"

    fog = iem.to_awc_payload(IEM_FOG, "SPECI")
    assert fog["metarType"] == "SPECI" and fog["wdir"] == "VRB" and fog["wxString"] == "FG"
    assert fog["clouds"] == [{"cover": "OVX", "base": 100}] and fog["fltCat"] == "LIFR"


@pytest.mark.parametrize("ceiling, vis, cat", [
    (None, 10, "VFR"), (3000, 10, "MVFR"), (3100, 5, "MVFR"), (999, 10, "IFR"),
    (5000, 2.5, "IFR"), (400, 10, "LIFR"), (None, 0.5, "LIFR"), (None, None, None),
])
def test_flight_category_matches_awc_thresholds(ceiling, vis, cat):
    assert iem.flight_category(ceiling, vis) == cat


def test_backfill_upsert_never_overwrites_primary_rows(con, monkeypatch):
    monkeypatch.setattr(warehouse, "BULK_THRESHOLD", 2)  # exercise the bulk path
    awc = [{"icao": "EHAM", "obs_time": 1, "fetched_at": warehouse.utcnow(), "payload": {"from": "awc"}}]
    warehouse.upsert(con, "raw.metar", awc, ["icao", "obs_time"])
    archive = [{"icao": "EHAM", "obs_time": t, "fetched_at": warehouse.utcnow(),
                "payload": {"from": "iem", "t": t}} for t in (1, 2, 3, 3)]
    warehouse.upsert(con, "raw.metar", archive, ["icao", "obs_time"], overwrite=False)
    rows = con.execute("select obs_time, payload ->> 'from' from raw.metar order by 1").fetchall()
    assert rows == [(1, "awc"), (2, "iem"), (3, "iem")]


def test_aerodatabox_windows_cover_the_utc_day_within_12_hours():
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo
    plain = aerodatabox.windows(date(2026, 6, 1), ZoneInfo("Asia/Hong_Kong"))
    assert [(a.isoformat(), b.isoformat()) for a, b in plain] == [
        ("2026-06-01T08:00:00", "2026-06-01T19:59:00"), ("2026-06-01T20:00:00", "2026-06-02T07:59:00")]
    # Sydney's clocks go forward at 02:00 local on 4 Oct 2026 (16:00 UTC on the 3rd): that
    # UTC half-day spans 13 local hours (22:00 to 11:00), so it is split.
    dst = aerodatabox.windows(date(2026, 10, 3), ZoneInfo("Australia/Sydney"))
    assert len(dst) == 3
    assert all(b - a < timedelta(hours=12) for a, b in dst)


def test_aerodatabox_rows_fill_in_the_boards_own_airport():
    from datetime import datetime, timezone
    board = {"departures": [{
        "movement": {"airport": {"icao": "YMML"}, "scheduledTime": {"utc": "2026-09-21 01:00Z"},
                     "revisedTime": {"utc": "2026-09-21 01:20Z"}},
        "number": "QF 400", "callSign": "QFA400", "status": "Departed",
        "aircraft": {"modeS": "7C6B2D"}}], "arrivals": None}
    (row,) = aerodatabox.flight_rows("YSSY", board)
    assert row["flight_id"] == "QF400@2026-09-21T01:00Z" and row["icao24"] == "7c6b2d"
    assert row["payload"]["departure"]["airport"]["icao"] == "YSSY"
    revised = datetime(2026, 9, 21, 1, 20, tzinfo=timezone.utc)
    assert row["departure_epoch"] == int(revised.timestamp()) and row["arrival_epoch"] is None


def test_iem_report_types_follow_the_stations_routine_minutes():
    # Sydney reports routinely at :00 and :30 (IEM calls the :30 ones specials); the
    # 04:47 report is a real special.
    valid = [f"2023-10-05 {h:02d}:{m}" for h in range(6) for m in ("00", "30")] + ["2023-10-05 04:47"]
    types = iem.metar_types([{"valid": v} for v in valid])
    assert types[:-1] == ["METAR"] * 12 and types[-1] == "SPECI"


def test_weather_lookback_covers_the_gap_between_runs(con, monkeypatch):
    from datetime import timedelta
    windows, taf_times = [], []
    monkeypatch.setattr(aviationweather.time, "sleep", lambda _: None)
    monkeypatch.setattr(aviationweather, "fetch_metar",
                        lambda icaos, hours, end=None: windows.append((end - timedelta(hours=hours), end)) or [])
    monkeypatch.setattr(aviationweather, "fetch_taf",
                        lambda icaos, at=None: taf_times.append(at) or [])
    aviationweather.ingest_metar(con, ["EHAM"], 26)
    aviationweather.ingest_taf(con, ["EHAM"], 26)
    # 6-hour chunks back from now, contiguous, 26 hours in all.
    assert all(b[0] == a[1] for a, b in zip(windows[1:], windows))
    assert windows[0][1] - windows[-1][0] == timedelta(hours=26)
    assert max(b - a for a, b in windows) <= timedelta(hours=6)
    # The current TAFs, then the ones current at each of the last 26 hours.
    assert taf_times[0] is None and len(taf_times) == 27


def test_a_day_counts_as_complete_from_06_utc():
    from datetime import datetime, timezone
    assert warehouse.newest_complete_day(1, datetime(2026, 10, 5, 5, 59, tzinfo=timezone.utc)) == 2
    assert warehouse.newest_complete_day(1, datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)) == 1


class _FakeOpenSky:
    calls: list = []

    def __init__(self, min_credits_remaining=200):
        self.credits_remaining, self.statuses = {}, {}

    def flights(self, direction, airport, begin, end):
        _FakeOpenSky.calls.append((airport, direction, begin))
        return []

    def track(self, icao24, at):
        return None


def test_opensky_fetches_only_missing_slots(con, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(opensky, "OpenSkyClient", _FakeOpenSky)
    monkeypatch.setattr(warehouse, "utcnow", lambda: datetime(2026, 10, 5, 7, tzinfo=timezone.utc))
    _FakeOpenSky.calls = []
    # OpenSky already has YSSY arrivals for 3 Oct: it must not spend credits on them again.
    warehouse.mark_slot(con, "opensky", "YSSY", "arrival", datetime(2026, 10, 3).date(), 5)
    # AeroDataBox has EHAM departures for 3 Oct, but OpenSky is preferred: fetch them anyway.
    warehouse.mark_slot(con, "aerodatabox", "EHAM", "departure", datetime(2026, 10, 3).date(), 5)
    cfg = {"days_back": 1, "backfill_days": 3, "max_backfill_calls_per_run": 1000}
    stats = opensky.ingest(con, ["YSSY", "EHAM"], cfg)
    # 3 days x 2 airports x 2 directions, less the slot OpenSky has.
    assert stats["slots"] == 11 and len(_FakeOpenSky.calls) == 11
    oct3 = int(datetime(2026, 10, 3, tzinfo=timezone.utc).timestamp())
    assert ("YSSY", "arrival", oct3) not in _FakeOpenSky.calls
    assert ("EHAM", "departure", oct3) in _FakeOpenSky.calls
    # Newest first: 4 Oct before 3 Oct before 2 Oct.
    assert _FakeOpenSky.calls[0][2] > _FakeOpenSky.calls[-1][2]

    _FakeOpenSky.calls = []
    assert opensky.ingest(con, ["YSSY", "EHAM"], cfg)["slots"] == 0, "the next hourly run has nothing to fetch"


def test_aerodatabox_spends_units_only_on_missing_days(con, monkeypatch):
    from datetime import datetime, timezone

    class FakeClient:
        def __init__(self, provider):
            self.calls = 0

        def fids(self, icao, start, end):
            self.calls += 1
            return {}

    monkeypatch.setattr(aerodatabox, "AeroDataBoxClient", FakeClient)
    monkeypatch.setattr(warehouse, "utcnow", lambda: datetime(2026, 10, 5, 7, tzinfo=timezone.utc))
    zones = {"YSSY": "Australia/Sydney", "EHAM": "Europe/Amsterdam"}
    # OpenSky failed this run, so AeroDataBox fills every day OpenSky has not loaded.
    # Without a configured cap: one new day for both airports (2 calls each), no more.
    first = aerodatabox.ingest(con, zones, {"backfill_days": 30}, opensky_ok=False)
    assert first["days"] == 2 and "budget" in first["stopped_early"]
    cfg = {"backfill_days": 3, "max_backfill_calls_per_run": 100}
    second = aerodatabox.ingest(con, zones, cfg, opensky_ok=False)
    assert second["days"] == 4, "2 Oct and 3 Oct for both airports; 4 Oct is already loaded"
    assert aerodatabox.ingest(con, zones, cfg, opensky_ok=False)["days"] == 0


def test_aerodatabox_falls_back_only_where_opensky_has_no_flights(con, monkeypatch):
    from datetime import datetime, timezone

    class FakeClient:
        def __init__(self, provider):
            self.calls = 0

        def fids(self, icao, start, end):
            self.calls += 1
            return {}

    monkeypatch.setattr(aerodatabox, "AeroDataBoxClient", FakeClient)
    monkeypatch.setattr(warehouse, "utcnow", lambda: datetime(2026, 10, 5, 7, tzinfo=timezone.utc))
    zones = {"YSSY": "Australia/Sydney", "EHAM": "Europe/Amsterdam"}
    oct3, oct4 = datetime(2026, 10, 3).date(), datetime(2026, 10, 4).date()
    for icao in zones:
        for direction in ("arrival", "departure"):
            warehouse.mark_slot(con, "opensky", icao, direction, oct4, 300)
    # OpenSky saw no YSSY arrivals on 3 Oct: a receiver gap, so AeroDataBox fills the day.
    warehouse.mark_slot(con, "opensky", "YSSY", "arrival", oct3, 0)
    warehouse.mark_slot(con, "opensky", "YSSY", "departure", oct3, 250)
    cfg = {"backfill_days": 3, "max_backfill_calls_per_run": 100}
    # OpenSky working: only YSSY 3 Oct. EHAM 3 Oct and both 2 Oct are left for OpenSky.
    assert aerodatabox.ingest(con, zones, cfg)["days"] == 1
    assert aerodatabox.done_days(con, "YSSY") == {oct3}
    # OpenSky failed: the days it has not loaded too, never the ones it has flights for.
    assert aerodatabox.ingest(con, zones, cfg, opensky_ok=False)["days"] == 3
    assert aerodatabox.done_days(con, "EHAM") == {datetime(2026, 10, 2).date(), oct3}

    assert not aerodatabox.needed({"arrival": 1, "departure": 1}, opensky_ok=False)
    assert aerodatabox.needed({"arrival": 0, "departure": 9}, opensky_ok=True)
    assert not aerodatabox.needed({}, opensky_ok=True)
    assert aerodatabox.needed({}, opensky_ok=False)
