"""ATIS page reduction in sources/atis_hk.py, against a capture of the real CAD page
(tests/fixtures/hk_atis.html) and invented HTML for the edge cases."""
from pathlib import Path

from aviation import warehouse
from aviation.sources import atis_hk

CAPTURE = (Path(__file__).parent / "fixtures" / "hk_atis.html").read_text(encoding="utf-8", errors="replace")
PAGE = """<html><head><title>ATIS</title><style>p {color: red}</style>
<script>var x = "VHHH ARR ATIS Z 0000Z";</script></head>
<body><h1>Hong Kong International Airport</h1>
<div>VHHH ARR ATIS B 0930Z.<br>RWY 07R<br/>  WIND 090/08   KT </div>
<p>VHHH&nbsp;DEP ATIS C 0931Z.</p><p>Remarks:</p><p>disclaimer</p></body></html>"""


def test_real_page_has_both_broadcasts_and_drops_the_disclaimer():
    row = atis_hk.row(CAPTURE)
    assert row["arrival_letter"] and len(row["arrival_letter"]) == 1
    assert row["departure_letter"] and len(row["departure_letter"]) == 1
    assert "VHHH ARR ATIS" in row["text"] and "VHHH DEP ATIS" in row["text"]
    assert "Remarks" not in row["text"] and "QNH" in row["text"]
    assert 100 < len(row["text"]) < 1500


def test_text_drops_script_and_style_and_keeps_lines():
    text = atis_hk.html_to_text(PAGE)
    assert "ATIS Z" not in text and "color" not in text
    assert text.splitlines()[1:4] == ["VHHH ARR ATIS B 0930Z.", "RWY 07R", "WIND 090/08 KT"]


def test_letters_and_unrelated_text():
    text = atis_hk.html_to_text(PAGE)
    assert atis_hk.letters(text) == {"ARR": "B", "DEP": "C"}
    assert atis_hk.letters("no broadcast here") == {"ARR": None, "DEP": None}


def test_row_is_none_for_an_empty_page():
    assert atis_hk.row("<html><script>1</script></html>") is None


def test_same_text_is_stored_once_and_keeps_first_seen(tmp_path):
    con = warehouse.connect(str(tmp_path / "t.duckdb"))
    for _ in range(2):
        warehouse.upsert(con, "raw.atis", [atis_hk.row(PAGE)], ["icao", "text_hash"],
                         keep_on_conflict=("first_seen_at",))
    assert con.execute("select count(*) from raw.atis").fetchone()[0] == 1
    first, last = con.execute("select first_seen_at, last_seen_at from raw.atis").fetchone()
    assert last >= first
    warehouse.upsert(con, "raw.atis", [atis_hk.row(PAGE.replace("07R", "07L"))],
                     ["icao", "text_hash"], keep_on_conflict=("first_seen_at",))
    assert con.execute("select count(*) from raw.atis").fetchone()[0] == 2
