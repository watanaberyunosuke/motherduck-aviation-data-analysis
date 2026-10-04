"""NOTAMs from the FAA NOTAM Search website, for airports with no free official feed.

Page:   https://notams.aim.faa.gov/notamSearch/nsapp.html
Search: POST https://notams.aim.faa.gov/notamSearch/search  (form-encoded)
        searchType=0&designatorsForLocation=EHAM&offset=0&notamsOnly=false
        -> {"notamList": [...], "startRecordCount": 1, "endRecordCount": 30,
            "totalNotamCount": 70, "error": "", ...}

This is the website's own backend, not a published API: no key, but no contract either.
Akamai bot detection refuses plain HTTP clients (403 even for the HTML page), so the
session uses curl_cffi to present Chrome's TLS/HTTP2 fingerprint and loads the page
first for its cookies. If it starts failing, the official FAA NOTAM API
(notam_faa.py, needs a key) is the fallback.

Each record carries the full ICAO text in `icaoMessage` for international NOTAMs, which
the HK parser reads as is. US domestic NOTAMs may only have `traditionalMessage`; then
the structured dates are used. Dropped: US DoD "V" series NOTAMs (keyword MILITARY),
which republish the issuing state's own procedure changes, and FAA Letters to Airmen
(keyword LTA), which are notices rather than NOTAMs. Results come 30 per page; every page is fetched, with a pause between.
"""
from __future__ import annotations

import html
import time
from datetime import datetime, timezone

import duckdb

from aviation import warehouse
from aviation.http import DEFAULT_TIMEOUT
from aviation.parsing.notam import ParsedNotam, parse

PAGE_URL = "https://notams.aim.faa.gov/notamSearch/nsapp.html"
SEARCH_URL = "https://notams.aim.faa.gov/notamSearch/search"
SOURCE = "faa_search"
MAX_PAGES = 20      # 600 NOTAMs; a busy airport has 50-150
PAUSE_SECONDS = 1.5  # between requests, to stay a light user of a public website
DROP_KEYWORDS = {"MILITARY", "LTA"}


class SchemaMismatch(RuntimeError):
    """Raised when the response no longer matches the shape seen on 2026-10-04."""


class Blocked(RuntimeError):
    """Raised when Akamai refuses the request (HTML error page instead of JSON)."""


def _session():
    # Imported here so the rest of the package does not need curl_cffi to import.
    from curl_cffi import requests as cffi_requests

    s = cffi_requests.Session(impersonate="chrome")
    r = s.get(PAGE_URL, timeout=DEFAULT_TIMEOUT)
    if r.status_code != 200:
        raise Blocked(f"NOTAM Search page returned {r.status_code}; Akamai may be blocking this client")
    return s


def fetch(session, icao: str) -> list[dict]:
    """Every NOTAM record for one location, across pages."""
    items: list[dict] = []
    for page in range(MAX_PAGES):
        if page:
            time.sleep(PAUSE_SECONDS)
        r = session.post(
            SEARCH_URL, timeout=DEFAULT_TIMEOUT,
            headers={"Accept": "application/json, text/plain, */*", "Referer": PAGE_URL},
            data={"searchType": "0", "designatorsForLocation": icao,
                  "offset": str(len(items)), "notamsOnly": "false"},
        )
        if r.status_code == 403 or "json" not in (r.headers.get("content-type") or ""):
            raise Blocked(f"NOTAM Search returned {r.status_code} "
                          f"({r.headers.get('content-type')}) for {icao}")
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, dict) or "notamList" not in payload:
            keys = list(payload) if isinstance(payload, dict) else type(payload).__name__
            raise SchemaMismatch(f"Expected a 'notamList' key for {icao}; got {keys}")
        if payload.get("error"):
            raise RuntimeError(f"NOTAM Search error for {icao}: {payload['error']}")
        batch = payload["notamList"]
        items += batch
        if not batch or len(items) >= int(payload.get("totalNotamCount") or 0):
            return items
    raise RuntimeError(f"{icao}: more than {MAX_PAGES} pages of NOTAMs; raise MAX_PAGES")


def _clean(text: str | None) -> str:
    """Entities (&#8203;) decoded, zero-width spaces and edge whitespace removed."""
    return html.unescape(text or "").replace("​", "").strip()


def _date(value: str | None) -> datetime | None:
    """'12/31/2026 2300EST' -> 2026-12-31 23:00 UTC. None for PERM or anything else."""
    try:
        return datetime.strptime((value or "").removesuffix("EST").strip(), "%m/%d/%Y %H%M") \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def rows(icao: str, items: list[dict], now: datetime) -> list[dict]:
    out: dict[str, dict] = {}
    for rec in items:
        if "notamNumber" not in rec:
            raise SchemaMismatch(f"NOTAM record for {icao} has no notamNumber; keys: {list(rec)}")
        if rec.get("keyword") in DROP_KEYWORDS or rec.get("cancelledOrExpired"):
            continue
        icao_text = _clean(rec.get("icaoMessage"))
        raw_text = icao_text or _clean(rec.get("traditionalMessage"))
        p = parse(icao_text) if icao_text else ParsedNotam()
        number = p.number or rec["notamNumber"]
        location = rec.get("icaoId") or p.location or icao
        end = (rec.get("endDate") or "").strip()
        key = f"{location}:{number}"
        out[key] = {
            "source": SOURCE,
            "notam_key": key,
            **p.as_dict(),
            # What the ICAO text gave wins; the record's own fields fill the gaps.
            "number": number,
            "location": location,
            "starts_at": p.starts_at or _date(rec.get("startDate")),
            "ends_at": p.ends_at or _date(end),
            "is_permanent": p.is_permanent if p.is_permanent is not None else end == "PERM",
            "is_estimated": p.is_estimated if p.is_estimated is not None else end.endswith("EST"),
            "body": p.body or _clean(rec.get("traditionalMessageFrom4thWord")) or raw_text or None,
            "raw_text": raw_text,
            "first_seen_at": now,
            "last_seen_at": now,
            "payload": rec,
        }
    return list(out.values())


def ingest(con: duckdb.DuckDBPyConnection, icaos: list[str]) -> int:
    # Fetch every airport before writing, with one timestamp, so a run either refreshes
    # all of them or none: stg_notam's in_latest_feed compares last_seen_at per source.
    session = _session()
    now = warehouse.utcnow()
    data, fetched = [], 0
    for n, icao in enumerate(icaos):
        if n:
            time.sleep(PAUSE_SECONDS)
        items = fetch(session, icao)
        fetched += len(items)
        data += rows(icao, items, now)
    written = warehouse.upsert(con, "raw.notam", data, ["source", "notam_key"],
                               keep_on_conflict=("first_seen_at",))
    warehouse.log_run(con, "notam_faa_search", written,
                      detail=f"{len(icaos)} airports, {fetched} records, "
                             f"{fetched - written} military, LTA or duplicate dropped")
    return written
