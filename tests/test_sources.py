import json
from pathlib import Path

import pytest

from aviation import warehouse
from aviation.sources import aviationweather, notam_faa, notam_hk, notam_rapidapi, opensky

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
    in_scope = {"est_departure_airport": "YSSY", "est_arrival_airport": "YMML"}
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
