"""Hong Kong's flight boards, for gate times: when each departure left its gate and each
arrival reached its stand.

Source: Airport Authority Hong Kong open data (data.gov.hk "Flight Information"), the same
boards /api/schedule serves live (api/index.py). Free, no key. One request per board and
local day: departures and arrivals, passenger and cargo. Past days stay available with
their final statuses (40 days back was served in Oct 2026).

The status carries the gate time:
  departures  "Dep 13:44"      off-block (pushback), not take-off: in Oct 2026 aircraft
                               were seen taxiing on ADS-B 10-20 min after it
  arrivals    "At gate 14:01"  on-block
  either      "... (07/10/2026)" after the time when it falls on another day than the board's
dbt pairs these with OpenSky's take-off and landing for taxi times (fct_taxi_times).

A request for one day answers with that day's board, and for recent days also the day
before's flights still listed (late ones) and the day after's. Each row keeps the day it
was requested for (loaded_for), which is what counts as loaded.

Each run reloads today and yesterday (their statuses still change) and up to
BACKFILL_DAYS_PER_RUN older days of the last WINDOW_DAYS not yet loaded after they ended,
newest first, so a new warehouse fills the window within a few hourly runs.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from zoneinfo import ZoneInfo

import duckdb

from aviation import warehouse
from aviation.http import DEFAULT_TIMEOUT, session

log = logging.getLogger(__name__)

URL = "https://www.hongkongairport.com/flightinfo-rest/rest/flights"
ICAO = "VHHH"
TZ = ZoneInfo("Asia/Hong_Kong")
WINDOW_DAYS = 31
BACKFILL_DAYS_PER_RUN = 10
BOARDS = [(arrival, cargo) for arrival in (False, True) for cargo in (False, True)]

# Status prefix holding the gate time, per direction.
GATE_PREFIX = {"departure": "Dep", "arrival": "At gate"}
_TIME = re.compile(r"(\d{1,2}):(\d{2})(?:\s*\((\d{2})/(\d{2})/(\d{4})\))?")
# Operating flight number: two-character IATA airline code, then the number.
_FLIGHT_NO = re.compile(r"([A-Z0-9]{2})\s*0*(\d{1,4})")


def _local(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def gate_time(status: str | None, direction: str, day: date) -> datetime | None:
    """The gate time in a status ("Dep 13:44", "At gate 23:08 (06/10/2026)"), else None."""
    prefix = GATE_PREFIX[direction]
    if not status or not status.startswith(prefix):
        return None
    m = _TIME.search(status[len(prefix):])
    if not m:
        return None
    hh, mm, dd, mo, yyyy = m.groups()
    on = date(int(yyyy), int(mo), int(dd)) if dd else day
    return _local(on, int(hh), int(mm))


def rows(payload: list[dict], arrival: bool, cargo: bool, requested: date) -> list[dict]:
    """One answer's flights (every day board in it) as raw.hkia_flights rows."""
    direction = "arrival" if arrival else "departure"
    now = warehouse.utcnow()
    out = []
    for board in payload:
        day = date.fromisoformat(board["date"])
        for f in board.get("list") or []:
            numbers = [n["no"].replace(" ", "") for n in f.get("flight") or []]
            if not numbers:
                continue
            hh, mm = (int(x) for x in f["time"].split(":"))
            status = (f.get("status") or "").strip() or None
            operating = f["flight"][0]
            airline = (operating.get("airline") or "").strip()
            m = _FLIGHT_NO.fullmatch(operating["no"].strip())
            out.append({
                "direction": direction,
                "flight": numbers[0],
                "scheduled_at": _local(day, hh, mm),
                # As ADS-B callsigns once leading zeros are dropped: CPA0710 and CPA710 alike.
                "callsign": f"{airline}{m.group(2)}" if len(airline) == 3 and m else None,
                "is_cargo": cargo,
                "status": status,
                "gate_at": gate_time(status, direction, day),
                "board_date": day,
                "loaded_for": requested,
                "fetched_at": now,
                "payload": f,
            })
    return out


def fetch(day: date, arrival: bool, cargo: bool) -> list[dict]:
    r = session().get(URL, timeout=DEFAULT_TIMEOUT, params={
        "span": 1, "date": day.isoformat(), "lang": "en",
        "cargo": str(cargo).lower(), "arrival": str(arrival).lower()})
    r.raise_for_status()
    return r.json()


def days_to_load(con: duckdb.DuckDBPyConnection, today: date) -> list[date]:
    """Today, yesterday, then older days of the window not loaded since they ended."""
    final = {d for (d,) in con.execute("""
        select loaded_for from raw.hkia_flights
        group by loaded_for
        -- Loaded after the next UTC midnight, 8 hours after the local day ended: its
        -- statuses are final. Epochs, so the session time zone does not matter.
        having epoch(max(fetched_at)) >= epoch(loaded_for + interval 1 day)
    """).fetchall()}
    older = [today - timedelta(days=n) for n in range(2, WINDOW_DAYS)]
    return [today, today - timedelta(days=1)] + [d for d in older if d not in final][:BACKFILL_DAYS_PER_RUN]


def ingest(con: duckdb.DuckDBPyConnection) -> str:
    today = datetime.now(TZ).date()
    loaded, failures = [], []
    for day in days_to_load(con, today):
        for arrival, cargo in BOARDS:
            try:
                loaded += rows(fetch(day, arrival, cargo), arrival, cargo, day)
            except Exception as exc:  # keep the boards that did load
                failures.append(f"{day} {'cargo ' if cargo else ''}{'arrivals' if arrival else 'departures'}: {exc}")
    if failures and not loaded:
        raise RuntimeError("; ".join(failures[:3]))
    for f in failures:
        log.warning("hkia: %s", f)
    # A late flight is in both its own day's answer and the next day's: keep its own day's.
    unique: dict[tuple, dict] = {}
    for r in loaded:
        key = (r["direction"], r["flight"], r["scheduled_at"])
        if key not in unique or r["loaded_for"] == r["board_date"]:
            unique[key] = r
    loaded = list(unique.values())
    n = warehouse.upsert(con, "raw.hkia_flights", loaded, ["direction", "flight", "scheduled_at"])
    days = sorted({r["loaded_for"] for r in loaded})
    with_gate = sum(r["gate_at"] is not None for r in loaded)
    detail = f"{len(days)} days {days[0]}..{days[-1]}, {with_gate} with gate time" if days else "no flights"
    if failures:
        detail += f", {len(failures)} boards failed"
    warehouse.log_run(con, "schedule_hk", n, detail=detail)
    return f"{n} ({detail})"
