"""NOTAMs from the FAA NOTAM API: US airports, plus international NOTAMs the FAA receives
through the ICAO NOTAM exchange (Amsterdam, Singapore).

Docs:     https://api.faa.gov/s/  (NOTAM API, free registration, client id + secret)
Endpoint: GET https://external-api.faa.gov/notamapi/v1/notams?icaoLocation=PANC
Auth:     `client_id` and `client_secret` request headers.

Response (GeoJSON, the default format):
    {"pageSize": 1000, "pageNum": 1, "totalCount": 61, "totalPages": 1, "items": [
        {"type": "Feature", "properties": {"coreNOTAMData": {
            "notam": {"id": "...", "number": "A1234/26", "type": "N", "selectionCode": "QMRLC",
                      "icaoLocation": "PANC", "effectiveStart": "2026-10-01T00:00:00.000Z",
                      "effectiveEnd": "PERM", "text": "...", "classification": "INTL"},
            "notamTranslation": [{"type": "ICAO", "formattedText": "A1234/26 NOTAMN\\nQ) ..."}]
        }}, "geometry": {...}}]}

The ICAO translation is parsed with the same parser as the HK feed, so Q-code fields come
out the same way; the structured fields fill whatever the text did not give. The API lists
NOTAMs that are active or not yet started, so cancellations show up as last_seen_at stopping.
"""
from __future__ import annotations

from datetime import datetime

import duckdb

from aviation import warehouse
from aviation.config import require_env
from aviation.http import DEFAULT_TIMEOUT, session
from aviation.parsing.notam import ParsedNotam, parse

URL = "https://external-api.faa.gov/notamapi/v1/notams"
SOURCE = "faa"
PAGE_SIZE = 1000  # the API's maximum
CREDENTIALS = ("FAA_CLIENT_ID", "FAA_CLIENT_SECRET")


class SchemaMismatch(RuntimeError):
    """Raised when the response no longer matches the documented shape."""


def fetch(icao: str) -> list[dict]:
    """Every item for one airport, across pages."""
    headers = {"client_id": require_env("FAA_CLIENT_ID"),
               "client_secret": require_env("FAA_CLIENT_SECRET")}
    s = session()
    items: list[dict] = []
    page = 1
    while True:
        r = s.get(URL, headers=headers, timeout=DEFAULT_TIMEOUT,
                  params={"icaoLocation": icao, "pageSize": PAGE_SIZE, "pageNum": page})
        if r.status_code == 429:
            raise RuntimeError("FAA NOTAM API rate limit reached (429).")
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, dict) or "items" not in payload:
            keys = list(payload) if isinstance(payload, dict) else type(payload).__name__
            raise SchemaMismatch(f"Expected an 'items' key for {icao}; got {keys}")
        items += payload["items"]
        if page >= int(payload.get("totalPages") or 1):
            return items
        page += 1


def _iso(value: str | None) -> datetime | None:
    if not value or value == "PERM":
        return None
    try:
        return datetime.fromisoformat(value.removesuffix("EST").strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def rows(icao: str, items: list[dict], now: datetime) -> list[dict]:
    out: dict[str, dict] = {}
    for item in items:
        try:
            core = item["properties"]["coreNOTAMData"]
            notam = core["notam"]
        except (KeyError, TypeError):
            keys = list(item) if isinstance(item, dict) else type(item).__name__
            raise SchemaMismatch(f"NOTAM item for {icao} has no coreNOTAMData.notam; keys: {keys}")
        icao_text = next((t.get("formattedText") for t in core.get("notamTranslation") or []
                          if t.get("type") == "ICAO" and t.get("formattedText")), None)
        raw_text = icao_text or notam.get("text") or ""
        p = parse(icao_text) if icao_text else ParsedNotam()
        number = p.number or notam.get("number") or notam.get("id")
        location = notam.get("icaoLocation") or p.location or icao
        end = notam.get("effectiveEnd") or ""
        key = f"{location}:{number}"
        out[key] = {
            "source": SOURCE,
            "notam_key": key,
            **p.as_dict(),
            # What the ICAO text gave wins; the structured fields fill the gaps.
            "number": number,
            "notam_type": p.notam_type or notam.get("type"),
            "q_code": p.q_code or notam.get("selectionCode"),
            "location": location,
            "starts_at": p.starts_at or _iso(notam.get("effectiveStart")),
            "ends_at": p.ends_at or _iso(end),
            "is_permanent": p.is_permanent if p.is_permanent is not None else end == "PERM",
            "is_estimated": p.is_estimated if p.is_estimated is not None else end.endswith("EST"),
            "body": p.body or notam.get("text"),
            "raw_text": raw_text,
            "first_seen_at": now,
            "last_seen_at": now,
            "payload": item,
        }
    return list(out.values())


def ingest(con: duckdb.DuckDBPyConnection, icaos: list[str]) -> int:
    # Fetch every airport before writing, with one timestamp, so a run either refreshes
    # all of them or none: stg_notam's in_latest_feed compares last_seen_at per source.
    now = warehouse.utcnow()
    data = [row for icao in icaos for row in rows(icao, fetch(icao), now)]
    n = warehouse.upsert(con, "raw.notam", data, ["source", "notam_key"],
                         keep_on_conflict=("first_seen_at",))
    warehouse.log_run(con, "notam_faa", n, detail=f"{len(icaos)} airports")
    return n
