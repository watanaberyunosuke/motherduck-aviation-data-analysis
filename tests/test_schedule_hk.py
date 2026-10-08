"""Hong Kong board parsing in sources/schedule_hk.py, against a capture of the real boards
(tests/fixtures/hkia_boards.json: one day requested, which answered with the day before's
late flights too, and for arrivals the day after's), and which days a run loads."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aviation import warehouse
from aviation.sources import schedule_hk

HKT = ZoneInfo("Asia/Hong_Kong")
CAPTURE = json.loads((Path(__file__).parent / "fixtures" / "hkia_boards.json").read_text())
REQUESTED = date.fromisoformat(CAPTURE["requested"])


def by_flight(rows):
    return {(r["flight"], r["board_date"]): r for r in rows}


def test_departures_left_gate_time_with_and_without_a_date():
    rows = by_flight(schedule_hk.rows(CAPTURE["departures"], False, False, REQUESTED))
    ze = rows[("ZE862", REQUESTED)]
    assert ze["status"] == "Dep 00:20" and ze["direction"] == "departure"
    assert ze["gate_at"] == datetime(2026, 10, 6, 0, 20, tzinfo=HKT)
    assert ze["scheduled_at"] == datetime(2026, 10, 6, 0, 5, tzinfo=HKT)
    assert ze["callsign"] == "ESR862"
    # Pushed back before midnight for a flight on the next day's board.
    cx = rows[("CX880", REQUESTED)]
    assert cx["gate_at"] == datetime(2026, 10, 5, 23, 56, tzinfo=HKT)
    # The day before's late flight, listed in this day's answer.
    hx = rows[("HX035", REQUESTED - timedelta(days=1))]
    assert hx["gate_at"] == datetime(2026, 10, 6, 1, 15, tzinfo=HKT)
    assert hx["loaded_for"] == REQUESTED and hx["board_date"] == REQUESTED - timedelta(days=1)


def test_arrivals_at_gate_time_and_statuses_without_one():
    rows = by_flight(schedule_hk.rows(CAPTURE["arrivals"], True, False, REQUESTED))
    cx = rows[("CX636", REQUESTED)]
    assert cx["gate_at"] == datetime(2026, 10, 6, 0, 3, tzinfo=HKT) and cx["callsign"] == "CPA636"
    # An estimate is not a gate time; a suffixed flight number is not a callsign.
    hx = rows[("HX018D", REQUESTED)]
    assert hx["status"].startswith("Est at") and hx["gate_at"] is None and hx["callsign"] is None


@pytest.mark.parametrize("status, direction, expected", [
    ("Dep 13:44", "departure", datetime(2026, 10, 7, 13, 44, tzinfo=HKT)),
    ("Dep 01:10 (08/10/2026)", "departure", datetime(2026, 10, 8, 1, 10, tzinfo=HKT)),
    ("At gate 14:01", "arrival", datetime(2026, 10, 7, 14, 1, tzinfo=HKT)),
    ("Landed 13:53", "arrival", None),
    ("At gate 14:01", "departure", None),
    ("Cancelled", "departure", None),
    (None, "arrival", None),
])
def test_gate_time(status, direction, expected):
    assert schedule_hk.gate_time(status, direction, date(2026, 10, 7)) == expected


def test_a_run_loads_today_yesterday_and_older_days_not_yet_final(tmp_path):
    con = warehouse.connect(str(tmp_path / "t.duckdb"))
    today = date(2026, 10, 7)
    row = {"direction": "departure", "flight": "CX1", "callsign": "CPA1", "is_cargo": False,
           "status": "Dep 10:00", "gate_at": None, "payload": {}}

    def loaded(day, fetched_at, flight):
        warehouse.upsert(con, "raw.hkia_flights", [{
            **row, "flight": flight, "scheduled_at": datetime(day.year, day.month, day.day, 10, tzinfo=HKT),
            "board_date": day, "loaded_for": day, "fetched_at": fetched_at}],
            ["direction", "flight", "scheduled_at"])

    # 5 Oct loaded after it ended; 4 Oct only while it was still running.
    loaded(date(2026, 10, 5), datetime(2026, 10, 6, 1, tzinfo=timezone.utc), "CX5")
    loaded(date(2026, 10, 4), datetime(2026, 10, 4, 12, tzinfo=timezone.utc), "CX4")
    days = schedule_hk.days_to_load(con, today)
    assert days[:2] == [today, date(2026, 10, 6)]
    assert date(2026, 10, 5) not in days and date(2026, 10, 4) in days
    assert len(days) == 2 + schedule_hk.BACKFILL_DAYS_PER_RUN
    assert days[2:] == sorted(days[2:], reverse=True)  # newest first
