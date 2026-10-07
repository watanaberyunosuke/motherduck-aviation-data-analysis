"""Direction of live aircraft in /api/live (api/index.py `direction`)."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("api_index", Path(__file__).parent.parent / "api" / "index.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

HKG = (22.308, 113.918)
BOTH = {"CPA710": {"inbound", "outbound"}}


def aircraft(nm_south: float, track: float, vrate=None, on_ground=False, callsign="CPA710") -> dict:
    # Due south of the airport: heading 0 points at it, 180 points away.
    return {"callsign": callsign, "lat": HKG[0] - nm_south * 1.852 / 111.2, "lon": HKG[1],
            "track_deg": track, "vrate_fpm": vrate, "on_ground": on_ground}


def direction(a, dirs=BOTH):
    return api.direction(a, *HKG, dirs)


@pytest.mark.parametrize("a, expected", [
    # Both ways, far out: the heading decides.
    (aircraft(100, 0), "inbound"),
    (aircraft(100, 180), "outbound"),
    # Both ways, near: a clear descent or climb beats the heading (downwind, holds).
    (aircraft(10, 180, vrate=-800), "inbound"),
    (aircraft(10, 0, vrate=1500), "outbound"),
    # Level near the airport: heading again.
    (aircraft(10, 180, vrate=0), "outbound"),
    (aircraft(10, 180, vrate=None), "outbound"),
    # On the ground: here, or somewhere else.
    (aircraft(1, 0, on_ground=True), "ground"),
    (aircraft(50, 0, on_ground=True), "other"),
])
def test_both_ways(a, expected):
    assert direction(a) == expected


def test_one_way_needs_the_track_to_agree_beyond_30_nm():
    inbound = {"CPA710": {"inbound"}}
    assert direction(aircraft(100, 0), inbound) == "inbound"
    assert direction(aircraft(100, 180), inbound) == "other"   # reused callsign flying away
    assert direction(aircraft(20, 180), inbound) == "inbound"  # manoeuvring near the airport
    outbound = {"CPA710": {"outbound"}}
    assert direction(aircraft(100, 180), outbound) == "outbound"
    assert direction(aircraft(100, 0), outbound) == "other"


def test_unknown_callsign_is_other():
    assert direction(aircraft(10, 0, callsign="ABC123")) == "other"
    assert direction(aircraft(10, 0, callsign=None)) == "other"


def test_freighters_by_cargo_operator_designator():
    ops = frozenset({"FDX", "CLX"})
    assert api.is_freighter("FDX5150", ops)
    assert api.is_freighter("CLX7", ops)
    # Passenger callsigns (which may carry belly cargo), registrations and blanks are not.
    assert not api.is_freighter("CPA101", ops)
    assert not api.is_freighter("FDX", ops)
    assert not api.is_freighter("B1234", ops)
    assert not api.is_freighter(None, ops)
