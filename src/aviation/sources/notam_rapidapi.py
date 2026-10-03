"""NOTAMs for airports with no free official feed (Australia, Singapore).

Provider: SkyLink API on RapidAPI  https://github.com/SkyLink-API/notam-api
Endpoint: GET https://skylink-api.p.rapidapi.com/v3/notams/{icao}
Paid, no free tier. Basic plan: $18.59/month for 5,000 requests, then $0.007 per request.
Currently disabled: no airport in config/airports.yml uses notam_source: rapidapi.

Documented response (their example, US airport):
    {"icao": "KJFK", "total_count": 23, "notams": [
        {"notam_id": "1/0001", "type": "N", "location": "KJFK",
         "effective": "2026-03-29T12:00:00Z", "expiration": "2026-04-15T23:59:00Z",
         "body": "...", "raw": "!JFK 03/001 ...", "source": "FAA SWIM FNS"}]}

Two things are NOT verified yet and should be checked on the first real call:
  1. Whether `raw` for YSSY/WSSS is ICAO format (Q-line present). US examples use FAA
     domestic format, which has no Q-line. The adapter prefers the provider's structured
     fields and only fills Q-code fields when an ICAO Q-line is found.
  2. Completeness versus the official source. This is a third-party reseller, not the
     issuing authority. Spot-check a few airports against Airservices NAIPS / CAAS.
"""
from __future__ import annotations

from datetime import datetime

import duckdb

from aviation import warehouse
from aviation.config import require_env
from aviation.http import DEFAULT_TIMEOUT, session
from aviation.parsing.notam import parse

HOST = "skylink-api.p.rapidapi.com"
SOURCE = "rapidapi"


class SchemaMismatch(RuntimeError):
    """Raised when the response no longer matches the documented shape."""


def fetch(icao: str) -> dict:
    r = session().get(
        f"https://{HOST}/v3/notams/{icao}",
        headers={"x-rapidapi-key": require_env("RAPIDAPI_KEY"), "x-rapidapi-host": HOST},
        timeout=DEFAULT_TIMEOUT,
    )
    if r.status_code == 429:
        raise RuntimeError("RapidAPI quota exhausted (429). Check the monthly budget in config.")
    r.raise_for_status()
    return r.json()


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def rows(icao: str, payload: dict) -> list[dict]:
    if not isinstance(payload, dict) or "notams" not in payload:
        keys = list(payload) if isinstance(payload, dict) else type(payload).__name__
        raise SchemaMismatch(f"Expected a 'notams' key for {icao}; got {keys}")

    now = warehouse.utcnow()
    out = []
    for rec in payload["notams"]:
        if "notam_id" not in rec:
            raise SchemaMismatch(f"NOTAM record for {icao} has no notam_id; keys: {list(rec)}")
        raw_text = rec.get("raw") or ""
        p = parse(raw_text)  # fills Q-code fields only when raw is ICAO format
        expiration = rec.get("expiration")
        out.append({
            "source": SOURCE,
            "notam_key": f"{rec.get('location') or icao}:{rec['notam_id']}",
            **p.as_dict(),
            # Provider's structured fields win over what we parsed.
            "number": rec["notam_id"],
            "notam_type": rec.get("type") or p.notam_type,
            "location": rec.get("location") or p.location or icao,
            "starts_at": _iso(rec.get("effective")) or p.starts_at,
            "ends_at": _iso(expiration) if expiration not in (None, "PERM") else p.ends_at,
            "is_permanent": True if expiration == "PERM" else p.is_permanent,
            "body": rec.get("body") or p.body,
            "raw_text": raw_text,
            "first_seen_at": now,
            "last_seen_at": now,
            "payload": rec,
        })
    return out


def ingest(con: duckdb.DuckDBPyConnection, icaos: list[str]) -> int:
    total = 0
    for icao in icaos:
        data = rows(icao, fetch(icao))
        total += warehouse.upsert(con, "raw.notam", data, ["source", "notam_key"],
                                  keep_on_conflict=("first_seen_at",))
    warehouse.log_run(con, "notam_rapidapi", total, detail=f"{len(icaos)} requests")
    return total
