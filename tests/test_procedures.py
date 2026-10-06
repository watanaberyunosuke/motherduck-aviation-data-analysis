"""AIRAC cycle arithmetic and link resolution in sources/procedures.py. The Singapore snippet
is the 'latest AIP' sentence as the AIM-SG page served it on 6 Oct 2026."""
from datetime import date

import pytest

from aviation import warehouse
from aviation.sources import procedures as pr

SG_PAGE = ('<p>To view/download the latest AIP, AIP AMDT, AIP SUP in HTML/PDF, click <a '
           'href="/aim-content/uploads/aip/01-OCT-2026/AIP-1/2026-09-03-000000/html/index-en-GB.html" '
           'target="_blank">here.</a></p><a href="/aip/">AIP</a>')


@pytest.mark.parametrize("day, cycle", [
    (date(2026, 10, 1), "2610"), (date(2026, 10, 28), "2610"), (date(2026, 10, 29), "2611"),
    (date(2026, 9, 3), "2609"), (date(2026, 1, 22), "2601"), (date(2026, 1, 21), "2513"),
    (date(2026, 11, 26), "2612"), (date(2026, 12, 24), "2613"),
])
def test_airac_cycle_matches_known_effective_dates(day, cycle):
    assert pr.airac_cycle(day) == cycle


def test_faa_results_url_uses_the_current_cycle():
    assert pr.faa_results_url("PANC", date(2026, 10, 6)).endswith("/search/results/?cycle=2610&ident=PANC")


def test_singapore_latest_eaip_link():
    assert pr.sg_latest_eaip(SG_PAGE) == ("https://aim-sg.caas.gov.sg/aim-content/uploads/aip/"
                                          "01-OCT-2026/AIP-1/2026-09-03-000000/html/index-en-GB.html")
    assert pr.sg_latest_eaip('<a href="/aip/">AIP</a>') is None


def test_resolve_falls_back_to_front_pages_when_singapore_fails(monkeypatch):
    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(pr, "session", lambda: type("S", (), {"get": staticmethod(boom)})())
    rows = {r["icao"]: r for r in pr.resolve(date(2026, 10, 6))}
    assert set(rows) == {"YSSY", "YMML", "YBBN", "WSSS", "VHHH", "EHAM", "PANC"}
    assert rows["WSSS"]["kind"] == "front page" and rows["WSSS"]["url"] == pr.SG_AIP
    assert rows["PANC"]["kind"] == "cycle" and "cycle=2610&ident=PANC" in rows["PANC"]["url"]
    assert rows["VHHH"]["url"] == "https://www.ais.gov.hk/", "Hong Kong is never deep-linked"


def test_ingest_overwrites_per_airport(tmp_path, monkeypatch):
    con = warehouse.connect(str(tmp_path / "t.duckdb"))
    monkeypatch.setattr(pr, "resolve", lambda day=None: [
        {"icao": "PANC", "name": "FAA d-TPP", "url": "u1", "kind": "cycle"}])
    pr.ingest(con)
    monkeypatch.setattr(pr, "resolve", lambda day=None: [
        {"icao": "PANC", "name": "FAA d-TPP", "url": "u2", "kind": "cycle"}])
    pr.ingest(con)
    assert con.execute("select url from raw.procedure_links").fetchall() == [("u2",)]
