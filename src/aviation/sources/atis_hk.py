"""Hong Kong ATIS from the Civil Aviation Department's public web page.

Page: https://atis.cad.gov.hk/ATIS/ATISweb/atis.php  (HKO and CAD's Internet ATIS for VHHH)

The page is server-rendered: its text holds an arrival and a departure broadcast, each
headed "VHHH ARR ATIS I 1006Z." / "VHHH DEP ATIS T 1007Z." (information letter, then the
issue time), followed by a "Remarks:" disclaimer. It is reduced to text up to "Remarks:",
the two letters are read from those headings, and the text is stored whole. A new row is
written only when the text changes, so the table holds each broadcast once, with when it
was first and last seen. tests/fixtures/hk_atis.html is a capture of the page.
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


def letters(text: str) -> dict[str, str | None]:
    """Information letter of the arrival and departure broadcasts, from their headings."""
    out: dict[str, str | None] = {"ARR": None, "DEP": None}
    for kind, letter in re.findall(r"\bVHHH\s+(ARR|DEP)\s+ATIS\s+([A-Z])\b", text.upper()):
        out[kind] = letter
    return out


def fetch() -> str:
    r = session().get(URL, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.text


def row(html: str) -> dict | None:
    """The ATIS row for this page, or None if it has no text."""
    text = html_to_text(html).split("Remarks:")[0].strip()[:MAX_TEXT]
    if not text:
        return None
    now = warehouse.utcnow()
    found = letters(text)
    return {"icao": ICAO, "text_hash": hashlib.sha1(text.encode()).hexdigest(),
            "arrival_letter": found["ARR"], "departure_letter": found["DEP"], "text": text,
            "first_seen_at": now, "last_seen_at": now}


def ingest(con: duckdb.DuckDBPyConnection) -> int | str:
    data = row(fetch())
    if data is None:
        return "page had no text"
    n = warehouse.upsert(con, "raw.atis", [data], ["icao", "text_hash"],
                         keep_on_conflict=("first_seen_at",))
    detail = f"arrival {data['arrival_letter']}, departure {data['departure_letter']}"
    warehouse.log_run(con, "atis_hk", n, detail=detail)
    return f"{n} ({detail})"
