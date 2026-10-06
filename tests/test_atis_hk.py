"""ATIS page reduction in sources/atis_hk.py. The HTML is invented to exercise the parsing;
the real page's markup was not available, so this pins behaviour, not the live page."""
from aviation import warehouse
from aviation.sources import atis_hk

PAGE = """<html><head><title>ATIS</title><style>p {color: red}</style>
<script>var x = "INFORMATION Z";</script></head>
<body><h1>Hong Kong International Airport</h1>
<div>HONG KONG ARRIVAL ATIS INFORMATION&nbsp;B<br>TIME 0930 RWY 07R<br/>  WIND 090/08   KT </div>
<p>QNH 1012</p></body></html>"""


def test_text_drops_script_and_style_and_keeps_lines():
    text = atis_hk.html_to_text(PAGE)
    assert "INFORMATION Z" not in text and "color" not in text
    assert text.splitlines()[1:] == ["HONG KONG ARRIVAL ATIS INFORMATION B",
                                     "TIME 0930 RWY 07R", "WIND 090/08 KT", "QNH 1012"]


def test_information_letter():
    assert atis_hk.info_letter(atis_hk.html_to_text(PAGE)) == "B"
    assert atis_hk.info_letter("no broadcast here") is None


def test_row_is_none_for_an_empty_page():
    assert atis_hk.row("<html><script>1</script></html>") is None


def test_same_text_is_stored_once_and_keeps_first_seen(tmp_path):
    con = warehouse.connect(str(tmp_path / "t.duckdb"))
    for _ in range(2):
        row = atis_hk.row(PAGE)
        warehouse.upsert(con, "raw.atis", [row], ["icao", "text_hash"],
                         keep_on_conflict=("first_seen_at",))
    assert con.execute("select count(*) from raw.atis").fetchone()[0] == 1
    first, last = con.execute("select first_seen_at, last_seen_at from raw.atis").fetchone()
    assert last >= first
    changed = atis_hk.row(PAGE.replace("1012", "1013"))
    warehouse.upsert(con, "raw.atis", [changed], ["icao", "text_hash"],
                     keep_on_conflict=("first_seen_at",))
    assert con.execute("select count(*) from raw.atis").fetchone()[0] == 2
