import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aviation.parsing.notam import CONDITION, SUBJECT_CATEGORY, parse

FIXTURES = Path(__file__).parent / "fixtures"
SEEDS = Path(__file__).parents[1] / "dbt" / "seeds"
HK = json.loads((FIXTURES / "hk_cad_notams.json").read_text())["notam"]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize("rec", HK, ids=[r["number"] for r in HK])
def test_every_live_hk_notam_parses_and_agrees_with_feed_times(rec):
    """The CAD feed publishes start/end separately from the text: use them as ground truth."""
    p = parse(rec["content"].replace(">", "\n"))
    assert p.number == rec["number"]
    assert p.q_code and p.location and p.body
    assert p.starts_at.strftime("%Y-%m-%d %H:%M") == rec["starttime"]
    if rec["endtime"] == "PERM":
        assert p.is_permanent and p.ends_at is None
    else:
        assert p.ends_at.strftime("%Y-%m-%d %H:%M") == rec["endtime"].replace(" EST", "")
        assert p.is_estimated == rec["endtime"].endswith("EST")
    assert not p.body.endswith(")"), "closing bracket of the NOTAM leaked into the body"


def test_runway_closure_fields():
    text = ("(A2231/26 NOTAMN\nQ) VHHK/QMRLC/IV/NBO/A /000/999/2219N11355E005\n"
            "A) VHHH B) 2609271500 C) 2609272359\nE) SOUTH RWY (RWY 07R/25L) CLSD FOR MAINT DUE WIP.)")
    p = parse(text)
    assert (p.fir, p.q_code, p.category, p.condition) == ("VHHK", "QMRLC", "runway", "closed")
    assert (p.traffic, p.purpose, p.scope) == ("IV", "NBO", "A")
    assert (p.centre_lat, p.centre_lon, p.radius_nm) == (22.3167, 113.9167, 5)
    assert p.starts_at == utc(2026, 9, 27, 15, 0) and p.ends_at == utc(2026, 9, 27, 23, 59)
    assert p.body == "SOUTH RWY (RWY 07R/25L) CLSD FOR MAINT DUE WIP."


def test_replacement_estimated_end_and_vertical_limits():
    text = ("(C0324/26 NOTAMR C0309/26\nQ) VHHK/QWULW/IV/BO /W /000/005/2217N11410E002\n"
            "A) VHHK B) 2609030422 C) 2609301000EST\nD) DLY 0000-1000\n"
            "E) UAS OPS WILL TAKE PLACE AT WAN CHAI NORTH.\nF) SFC G) 493FT AMSL)")
    p = parse(text)
    assert (p.notam_type, p.replaces) == ("R", "C0309/26")
    assert p.is_estimated and p.ends_at == utc(2026, 9, 30, 10, 0)
    assert p.schedule == "DLY 0000-1000"
    assert (p.lower_limit, p.upper_limit) == ("SFC", "493FT AMSL")
    assert p.body == "UAS OPS WILL TAKE PLACE AT WAN CHAI NORTH."


def test_item_letters_inside_free_text_are_not_treated_as_items():
    text = ("(A0001/26 NOTAMN\nQ) VHHK/QMXLC/IV/BO /A /000/999/2219N11355E005\n"
            "A) VHHH B) 2609281801 C) 2609282330\nE) TWY C) AND TWY B) CLSD. SEE D) BELOW)")
    p = parse(text)
    assert p.ends_at == utc(2026, 9, 28, 23, 30)
    assert p.schedule is None
    assert p.body == "TWY C) AND TWY B) CLSD. SEE D) BELOW"


def test_southern_and_western_hemispheres_are_negative():
    text = ("(C1234/26 NOTAMN\nQ) YMMM/QMRLC/IV/NBO/A /000/999/3740S14451E005\n"
            "A) YMML B) 2610010000 C) PERM\nE) TEST)")
    p = parse(text)
    assert p.centre_lat == pytest.approx(-37.6667, abs=1e-4)
    assert p.centre_lon == pytest.approx(144.85, abs=1e-4)
    assert p.is_permanent


def test_non_icao_text_degrades_to_empty_fields_instead_of_raising():
    p = parse("!JFK 03/001 JFK RWY 04L/22R CLSD 2603291200-2604152359")
    assert p.q_code is None and p.starts_at is None and p.category is None


def test_dbt_seeds_match_python_q_code_maps():
    """The SQL layer reads the seeds; the parser reads the dicts. Keep them identical."""
    with open(SEEDS / "notam_q_subjects.csv") as f:
        assert {r["subject_code"]: r["category"] for r in csv.DictReader(f)} == SUBJECT_CATEGORY
    with open(SEEDS / "notam_q_conditions.csv") as f:
        assert {r["condition_code"]: r["condition"] for r in csv.DictReader(f)} == CONDITION
