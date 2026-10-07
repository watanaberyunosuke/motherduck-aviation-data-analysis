"""HKIA's flight boards as /api/schedule rows (api/index.py `hkia_flights`)."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

spec = importlib.util.spec_from_file_location("api_index", Path(__file__).parent.parent / "api" / "index.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

HKG = ZoneInfo("Asia/Hong_Kong")


def board(date, *flights):
    return [{"date": date, "arrival": True, "cargo": False, "list": list(flights)}]


def flight(time, status="", no="EK 382", airline="UAE", **extra):
    return {"time": time, "flight": [{"no": no, "airline": airline}, {"no": "AZ 5630", "airline": "AZA"}],
            "status": status, "statusCode": None, **extra}


def one(f, arrival=True, date="2026-10-06"):
    return api.hkia_flights(board(date, f), arrival=arrival, cargo=False, tz=HKG)[0]


def test_arrival_with_estimate_stand_and_codeshares():
    r = one(flight("15:25", "Est at 15:02", origin=["DXB"], baggage="9", hall="A", terminal="", stand="N62"))
    assert r["dir"] == "inbound"
    assert (r["flight"], r["codeshares"], r["callsign"]) == ("EK382", ["AZ5630"], "UAE382")
    assert r["other"] == "DXB"
    assert r["scheduled_at"] == "2026-10-06T07:25:00+00:00"
    assert r["estimated_at"] == "2026-10-06T07:02:00+00:00" and r["actual_at"] is None
    assert r["state"] == "estimated"
    assert (r["stand"], r["belt"], r["terminal"]) == ("N62", "9", None)


def test_actual_times_and_dates_that_differ_from_the_scheduled_day():
    r = one(flight("00:10", "At gate 23:08 (05/10/2026)", origin=["CVG", "LEJ"]))
    assert r["state"] == "at_gate"
    assert r["actual_at"] == "2026-10-05T15:08:00+00:00"
    assert r["other"] == "CVG" and r["route"] == ["CVG", "LEJ"]
    dep = one(flight("11:20", "Dep 11:57", destination=["TPE"], gate="44", terminal="T1"), arrival=False)
    assert (dep["dir"], dep["state"], dep["actual_at"], dep["gate"]) == ("outbound", "departed", "2026-10-06T03:57:00+00:00", "44")


def test_states_without_times():
    for status, state in [("", "scheduled"), ("Cancelled", "cancelled"), ("Delayed", "delayed"),
                          ("Boarding Soon", "boarding_soon"), ("Boarding", "boarding"),
                          ("Final Call", "final_call"), ("Gate Closed", "gate_closed"), ("Something new", "other")]:
        r = one(flight("12:00", status, destination=["NRT"]), arrival=False)
        assert r["state"] == state, status
        assert r["estimated_at"] is None and r["actual_at"] is None
        assert r["status"] == (status or None)


def test_callsign_drops_leading_zeros_and_skips_suffixed_numbers():
    assert one(flight("12:00", no="MM 067", airline="APJ"))["callsign"] == "APJ67"
    assert one(flight("12:00", no="CX 698X", airline="CPA"))["callsign"] is None
    assert one(flight("12:00", no="CX 698", airline=""))["callsign"] is None


def test_window_keeps_late_flights_by_their_estimate():
    now = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    old = one(flight("08:00", "", origin=["SIN"]), date="2026-10-03")
    late = one(flight("08:00", "Est at 15:00 (06/10/2026)", origin=["SIN"]), date="2026-10-03")
    assert not api._in_window(old, now)
    assert api._in_window(late, now)


def test_freighters_from_the_cargo_board_or_a_cargo_operator():
    passenger = one(flight("10:00", no="CX 710", airline="CPA"))
    cargo_board = api.hkia_flights(board("2026-10-06", flight("11:00", no="CX 2", airline="CPA")),
                                   arrival=True, cargo=True, tz=HKG)[0]
    operator = one(flight("12:00", no="FX 5150", airline="FDX"))
    api.tag_freighters([passenger, cargo_board, operator], frozenset({"FDX"}))
    assert [r["is_freighter"] for r in (passenger, cargo_board, operator)] == [False, True, True]
