"""Hong Kong ATIS from the Civil Aviation Department's public web page.

Page: https://atis.cad.gov.hk/ATIS/ATISweb/atis.php  (HKO and CAD's Internet ATIS for VHHH)

The page's markup was not available when this was written (the host is blocked from the
development sandbox), so nothing here depends on its structure: the page is reduced to
text, the information letter is read from "INFORMATION <letter>" if the text has one, and
the text is stored whole. A new row is written only when the text changes, so the table
holds each broadcast once, with when it was first and last seen.

Check the first run's `raw.ingest_log` row (`atis_hk`) and `raw.atis.text`: if the page
is navigation and boilerplate around the broadcast, or loads the broadcast by script, the
fetch needs to target the page's data URL instead.
"""
from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser

import duckdb

from aviation import warehouse
from aviation.http import DEFAULT_TIMEOUT, session

URL = "https://atis.cad.gov.hk/ATIS/ATISweb/atis.php"
ICAO = "VHHH"
# A broadcast is a few hundred characters; anything far longer is page furniture.
MAX_TEXT = 4000

_BLOCKS = {"br", "p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4", "pre"}
_SKIP = {"script", "style", "noscript", "title"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Visible text, one line per block element, runs of space collapsed."""
    parser = _Text()
    parser.feed(html)
    lines = (re.sub(r"[ \t\r\f\v ]+", " ", ln).strip() for ln in "".join(parser.parts).split("\n"))
    return "\n".join(ln for ln in lines if ln)


def info_letter(text: str) -> str | None:
    m = re.search(r"\bINFORMATION\s+([A-Z])\b", text.upper())
    return m.group(1) if m else None


def fetch() -> str:
    r = session().get(URL, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.text


def row(html: str) -> dict | None:
    """The ATIS row for this page, or None if it has no text."""
    text = html_to_text(html)[:MAX_TEXT]
    if not text:
        return None
    now = warehouse.utcnow()
    return {"icao": ICAO, "text_hash": hashlib.sha1(text.encode()).hexdigest(),
            "info_letter": info_letter(text), "text": text,
            "first_seen_at": now, "last_seen_at": now}


def ingest(con: duckdb.DuckDBPyConnection) -> int | str:
    data = row(fetch())
    if data is None:
        return "page had no text"
    n = warehouse.upsert(con, "raw.atis", [data], ["icao", "text_hash"],
                         keep_on_conflict=("first_seen_at",))
    warehouse.log_run(con, "atis_hk", n, detail=f"information {data['info_letter']}")
    return f"{n} (information {data['info_letter'] or 'letter not found'})"
