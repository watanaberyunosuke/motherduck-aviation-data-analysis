"""Hong Kong NOTAMs from the Civil Aviation Department public feed.

Page:  https://www.notam.ais.gov.hk/
Data:  https://www.notam.ais.gov.hk/data  -> {"notam": [...], "snowtam": [...]}

Each record carries `number`, `id`, `starttime`, `endtime` and the full ICAO text in
`content`, with ">" used as the line separator. The feed lists currently valid NOTAMs
only, so cancellations show up as a NOTAM's last_seen_at stopping.

The site's terms say the data is informational and "uncontrolled when printed, copied or
downloaded". Read https://www.notam.ais.gov.hk/ Important Notices before relying on it
for anything beyond analysis. Poll modestly: the workflow calls it every 3 hours.
"""
from __future__ import annotations

import duckdb

from aviation import warehouse
from aviation.http import DEFAULT_TIMEOUT, session
from aviation.parsing.notam import parse

URL = "https://www.notam.ais.gov.hk/data"
SOURCE = "hk_cad"


def fetch() -> dict:
    r = session().get(URL, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json()


def rows(payload: dict) -> list[dict]:
    now = warehouse.utcnow()
    out = []
    for rec in payload.get("notam", []):
        text = rec["content"].replace(">", "\n")
        p = parse(text)
        # Series letter + number is unique within the issuing office; the FIR prefix
        # keeps it unique if another source ever issues the same number.
        key = f"{p.fir or 'VHHK'}:{rec['number']}"
        out.append({
            "source": SOURCE,
            "notam_key": key,
            **p.as_dict(),
            "number": p.number or rec["number"],
            "raw_text": text,
            "first_seen_at": now,
            "last_seen_at": now,
            "payload": rec,
        })
    return out


def ingest(con: duckdb.DuckDBPyConnection) -> int:
    data = rows(fetch())
    n = warehouse.upsert(con, "raw.notam", data, ["source", "notam_key"],
                         keep_on_conflict=("first_seen_at",))
    warehouse.log_run(con, "notam_hk_cad", n)
    return n
