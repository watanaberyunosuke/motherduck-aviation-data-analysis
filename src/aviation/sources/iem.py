"""METAR archive from the Iowa Environmental Mesonet (IEM), the fallback weather source.

Docs: https://mesonet.agron.iastate.edu/request/download.phtml  (free, no key, global
METARs from about 2000; the request service is cgi-bin/request/asos.py)

The Aviation Weather Center API is the primary source, but it only serves the last 30
days. IEM fills the years before that, and stands in for the hourly fetch if AWC is down.
Its rows are reshaped into the AWC JSON payload (the fields stg_metar reads), with
"source": "iem" added, so both land in raw.metar and dbt treats them alike. AWC rows win:
IEM rows are only inserted where no row exists for (icao, obs_time).

What does not carry over: AWC's own fields (receiptTime, qcField, station name) and its
flight category, which is recomputed here from ceiling and visibility with the same
thresholds.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, timedelta, timezone

import duckdb

from aviation import warehouse
from aviation.http import session

log = logging.getLogger(__name__)

URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
FIELDS = ["tmpf", "dwpf", "drct", "sknt", "gust", "vsby", "alti", "wxcodes",
          "skyc1", "skyc2", "skyc3", "skyc4", "skyl1", "skyl2", "skyl3", "skyl4", "metar"]
# IEM's report types 3 (routine) and 4 (special). IEM decides "routine" by the US hourly
# slot, so a half-hourly station's :30 report comes back as special; metar_types() labels
# by the station's own routine minutes instead.
REPORT_TYPES = (3, 4)
# A year of one airport's METARs is a few MB; IEM can take a minute to build it.
TIMEOUT = 300


def fetch(icao: str, start: datetime, end: datetime) -> list[dict]:
    """IEM's CSV rows for one airport, every report observed in [start, end) UTC."""
    params = [("station", icao), ("tz", "Etc/UTC"), ("format", "onlycomma"),
              ("latlon", "no"), ("missing", "empty"), ("trace", "empty"),
              ("year1", start.year), ("month1", start.month), ("day1", start.day),
              ("hour1", start.hour), ("minute1", start.minute),
              ("year2", end.year), ("month2", end.month), ("day2", end.day),
              ("hour2", end.hour), ("minute2", end.minute)]
    params += [("data", f) for f in FIELDS] + [("report_type", str(t)) for t in REPORT_TYPES]
    r = session().get(URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def _num(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "", "M") else None
    except ValueError:
        return None


def _temp(group: str) -> int:
    return -int(group[1:]) if group.startswith("M") else int(group)


def flight_category(ceiling_ft: int | None, visibility_sm: float | None) -> str | None:
    """VFR / MVFR / IFR / LIFR from ceiling and visibility, as the AWC assigns it."""
    if ceiling_ft is None and visibility_sm is None:
        return None
    ceiling = ceiling_ft if ceiling_ft is not None else 99999
    vis = visibility_sm if visibility_sm is not None else 99.0
    if ceiling < 500 or vis < 1:
        return "LIFR"
    if ceiling < 1000 or vis < 3:
        return "IFR"
    if ceiling <= 3000 or vis <= 5:
        return "MVFR"
    return "VFR"


def to_awc_payload(row: dict, metar_type: str) -> dict | None:
    """One IEM CSV row as an AWC-style METAR payload, or None if it cannot be keyed."""
    raw = (row.get("metar") or "").strip()
    try:
        observed = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None
    if not raw:
        return None

    payload: dict = {
        "icaoId": row["station"],
        "obsTime": int(observed.timestamp()),
        "reportTime": observed.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "metarType": metar_type,
        "rawOb": f"{metar_type} {raw}",
        "source": "iem",
    }

    # Temperature, dew point and QNH from the report itself, as the AWC decodes them;
    # IEM's own columns are converted to Fahrenheit and inches.
    # US reports carry tenths of a degree in the remarks (T01720006 = 17.2 / 0.6), which
    # the AWC prefers over the body group.
    if m := re.search(r"\sT([01])(\d{3})(?:([01])(\d{3}))?(?=\s|$)", raw):
        payload["temp"] = (-1 if m.group(1) == "1" else 1) * int(m.group(2)) / 10
        if m.group(4):
            payload["dewp"] = (-1 if m.group(3) == "1" else 1) * int(m.group(4)) / 10
    elif m := re.search(r"\s(M?\d{2})/(M?\d{2})?(?=\s|$)", raw):
        payload["temp"] = _temp(m.group(1))
        if m.group(2):
            payload["dewp"] = _temp(m.group(2))
    elif (t := _num(row.get("tmpf"))) is not None:
        payload["temp"] = round((t - 32) * 5 / 9, 1)
    if m := re.search(r"\sQ(\d{4})(?=\s|$)", raw):
        payload["altim"] = int(m.group(1))
    elif (a := _num(row.get("alti"))) is not None:
        payload["altim"] = round(a * 33.8639, 1)

    if re.search(r"\sVRB\d{2,3}(G\d{2,3})?(KT|MPS)", raw):
        payload["wdir"] = "VRB"
    elif (d := _num(row.get("drct"))) is not None:
        payload["wdir"] = int(d)
    if (s := _num(row.get("sknt"))) is not None:
        payload["wspd"] = int(s)
    if (g := _num(row.get("gust"))) is not None:
        payload["wgst"] = int(g)

    # 9999 and CAVOK decode to 6.21 sm (10 km); the AWC writes those as "6+", and a US
    # "10SM" as "10+".
    vis = _num(row.get("vsby"))
    if vis is not None:
        if re.search(r"\s10SM(?=\s|$)", raw):
            payload["visib"] = "10+"
        else:
            payload["visib"] = "6+" if vis >= 6.2 else vis
    if row.get("wxcodes"):
        payload["wxString"] = row["wxcodes"]

    clouds = []
    for i in range(1, 5):
        cover = (row.get(f"skyc{i}") or "").strip()
        if not cover:
            continue
        cover = "OVX" if cover == "VV" else cover
        base = _num(row.get(f"skyl{i}"))
        clouds.append({"cover": cover, "base": int(base) if base is not None else None})
    if clouds:
        payload["clouds"] = clouds
    ceiling = min((c["base"] for c in clouds
                   if c["cover"] in ("BKN", "OVC", "OVX") and c["base"] is not None), default=None)
    payload["fltCat"] = flight_category(ceiling, vis)
    return payload


def metar_types(rows: list[dict]) -> list[str]:
    """METAR or SPECI for each row. A station reports routinely at fixed minutes (:00 and
    :30 in Sydney, :25 and :55 in Amsterdam), so a minute that recurs in at least 40% of
    the hours seen is routine; anything else is a special."""
    minutes = [(row.get("valid") or "")[-2:] for row in rows]
    hours = len({(row.get("valid") or "")[:13] for row in rows}) or 1
    counts = {m: minutes.count(m) for m in set(minutes)}
    routine = {m for m, c in counts.items() if c >= 0.4 * hours}
    return ["METAR" if m in routine else "SPECI" for m in minutes]


def metar_rows(rows: list[dict]) -> list[dict]:
    now = warehouse.utcnow()
    out = []
    for row, metar_type in zip(rows, metar_types(rows)):
        p = to_awc_payload(row, metar_type)
        if p:
            out.append({"icao": p["icaoId"], "obs_time": p["obsTime"], "fetched_at": now, "payload": p})
    return out


def ingest_metar(con: duckdb.DuckDBPyConnection, icaos: list[str], start: datetime,
                 end: datetime, chunk_days: int = 366) -> int:
    """Fill raw.metar from IEM for [start, end), leaving existing rows (AWC's) untouched."""
    n = 0
    for icao in icaos:
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            rows = metar_rows(fetch(icao, chunk_start, chunk_end))
            n += warehouse.upsert(con, "raw.metar", rows, ["icao", "obs_time"], overwrite=False)
            log.info("iem %s %s..%s: %d rows", icao, chunk_start.date(), chunk_end.date(), len(rows))
            chunk_start = chunk_end
    warehouse.log_run(con, "metar_iem", n, detail=f"{start.isoformat()}..{end.isoformat()}")
    return n
