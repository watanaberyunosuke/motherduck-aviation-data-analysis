import json
from pathlib import Path

import pytest

from aviation import warehouse
from aviation.sources import aviationweather, notam_hk, notam_rapidapi, opensky

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


def test_opensky_day_window_is_a_whole_utc_day():
    from datetime import datetime, timezone
    begin, end = opensky.day_window(1, now=datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc))
    assert datetime.fromtimestamp(begin, timezone.utc) == datetime(2026, 9, 26, tzinfo=timezone.utc)
    assert end - begin == 86400
