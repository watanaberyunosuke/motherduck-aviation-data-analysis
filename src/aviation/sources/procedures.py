"""Where to find each airport's official SID/STAR charts: links, never copies.

Each state publishes its charts as PDFs in its AIP. Chart URLs carry the AIRAC cycle, so
they change every 28 days; this finds the current entry point where the publisher's site
allows it and otherwise links the AIP's front page:

  PANC  FAA Terminal Procedures Search, results for the airport in the current cycle. The
        cycle (YYMM: year and AIRAC cycle number) is computed, the URL is
        .../dtpp/search/results/?cycle=2610&ident=PANC.
  WSSS  The "latest AIP" link on https://aim-sg.caas.gov.sg/aip/, read from that page.
  VHHH  The AIP front page. The CAD requires users to accept its Terms of Use there and
        its copyright notice bars reproducing the content, so the charts are not deep-linked.
  EHAM  eaip.lvnl.nl answers scripted requests with 403, so its front page.
  YSSY, YMML, YBBN  Airservices' AIP page; its DAP file names carry the cycle date and the
        page lists no stable link to them.

A resolver that fails keeps the front page. Rows land in raw.procedure_links.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from urllib.parse import urljoin

import duckdb

from aviation import warehouse
from aviation.http import DEFAULT_TIMEOUT, session

log = logging.getLogger(__name__)

FAA_SEARCH = "https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/dtpp/search/"
SG_AIP = "https://aim-sg.caas.gov.sg/aip/"

# icao -> (publisher shown in the Dive, front page)
FRONT_PAGES = {
    "YSSY": ("Airservices Australia AIP", "https://www.airservicesaustralia.com/aip/aip.asp"),
    "YMML": ("Airservices Australia AIP", "https://www.airservicesaustralia.com/aip/aip.asp"),
    "YBBN": ("Airservices Australia AIP", "https://www.airservicesaustralia.com/aip/aip.asp"),
    "WSSS": ("CAAS AIP Singapore", SG_AIP),
    "VHHH": ("Hong Kong AIP", "https://www.ais.gov.hk/"),
    "EHAM": ("LVNL eAIP", "https://eaip.lvnl.nl"),
    "PANC": ("FAA d-TPP", FAA_SEARCH),
}

# An AIRAC effective date; every cycle is a multiple of 28 days from it (1 Oct 2026 is
# cycle 10 of 2026, which the FAA calls 2610).
AIRAC_REFERENCE = date(2026, 10, 1)


def airac_cycle(day: date) -> str:
    """The AIRAC cycle in force on `day` as the FAA writes it: YYMM, year then cycle number."""
    effective = AIRAC_REFERENCE + timedelta(days=28 * ((day - AIRAC_REFERENCE).days // 28))
    first = effective
    while (first - timedelta(days=28)).year == effective.year:
        first -= timedelta(days=28)
    number = (effective - first).days // 28 + 1
    return f"{effective.year % 100:02d}{number:02d}"


def faa_results_url(ident: str, day: date) -> str:
    return f"{FAA_SEARCH}results/?cycle={airac_cycle(day)}&ident={ident}"


def sg_latest_eaip(html: str) -> str | None:
    """The eAIP index link the AIM-SG page offers for the latest AIP."""
    m = re.search(r'href="([^"]*/html/index-en-GB\.html)"', html)
    return urljoin(SG_AIP, m.group(1)) if m else None


def resolve(day: date | None = None) -> list[dict]:
    """One row per airport: (name, url, kind) where kind is 'cycle' for a link into the
    current cycle and 'front page' otherwise."""
    day = day or date.today()
    rows = {icao: {"icao": icao, "name": name, "url": url, "kind": "front page"}
            for icao, (name, url) in FRONT_PAGES.items()}
    rows["PANC"].update(url=faa_results_url("PANC", day), kind="cycle")
    try:
        r = session().get(SG_AIP, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        if link := sg_latest_eaip(r.text):
            rows["WSSS"].update(url=link, kind="cycle")
        else:
            log.warning("no latest-AIP link found on %s; keeping the front page", SG_AIP)
    except Exception as exc:
        log.warning("Singapore AIP page failed (%s); keeping the front page", exc)
    return list(rows.values())


def ingest(con: duckdb.DuckDBPyConnection) -> str:
    now = warehouse.utcnow()
    rows = [{**r, "checked_at": now} for r in resolve(now.date())]
    n = warehouse.upsert(con, "raw.procedure_links", rows, ["icao"])
    warehouse.log_run(con, "procedures", n)
    return f"{n} links ({sum(r['kind'] == 'cycle' for r in rows)} into the current cycle)"
