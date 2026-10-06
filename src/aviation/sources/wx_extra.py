"""Temperature, dew point, weather text and sunrise/sunset from outside the METAR.

The METAR stays primary (dbt/models/marts/fct_airport_conditions.sql); these fill in when
it is missing, stale or lacks a field. Sources, best first, one row each per run:

  1. gov         the airport's national weather service (config `weather_gov`)
                   hko  Hong Kong Observatory open data (temperature only)
                   nea  Singapore NEA via data.gov.sg (temperature only)
                   nws  US National Weather Service station observation (PANC)
                 None for the others: BoM's terms bar passing its data on, and KNMI serves
                 NetCDF files behind a key; both fall through to the sources below.
  2. open-meteo  model value at the airport's coordinates. Free tier is non-commercial.
  3. met.no      MET Norway Locationforecast, same idea. Needs an identifying User-Agent.

Sunrise and sunset: MET Norway's Sunrise API, else computed here from the coordinates.

None of these hosts is reachable from every sandbox and the response shapes below are
taken from each provider's documentation, so every parser reads defensively and a failure
only drops that source.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb

from aviation import warehouse
from aviation.http import DEFAULT_TIMEOUT, session

log = logging.getLogger(__name__)

# MET Norway blocks requests without a User-Agent that says who is calling.
MET_NO_USER_AGENT = "aviation-data-analysis/0.1 github.com/watanaberyunosuke/motherduck-aviation-data-analysis"

HKO_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php"
NEA_URL = "https://api-open.data.gov.sg/v2/real-time/api/air-temperature"
NWS_URL = "https://api.weather.gov/stations/{icao}/observations/latest"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
MET_NO_FORECAST_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
MET_NO_SUN_URL = "https://api.met.no/weatherapi/sunrise/3.0/sun"

# HKO lists temperature by place; this is the one at the airport.
HKO_AIRPORT_PLACE = "Chek Lap Kok"

# Open-Meteo's WMO weather_code, in METAR-ish words.
WMO_TEXT = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog",
    48: "Rime fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain", 71: "Light snow", 73: "Snow",
    75: "Heavy snow", 77: "Snow grains", 80: "Light showers", 81: "Showers",
    82: "Heavy showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm, hail", 99: "Thunderstorm, hail",
}


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _get_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict:
    r = session().get(url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _parse_time(text) -> datetime | None:
    """An ISO timestamp as UTC; a time with no offset is taken as UTC."""
    if not text:
        return None
    try:
        at = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


# ---- parsers: one per source, payload in, {observed_at, temp_c, dewpoint_c, wx_text} out ----

def parse_hko(payload: dict) -> dict | None:
    places = (payload.get("temperature") or {}).get("data") or []
    hit = next((p for p in places if p.get("place") == HKO_AIRPORT_PLACE), None)
    temp = _num(hit.get("value")) if hit else None
    if temp is None:
        return None
    observed = (payload.get("temperature") or {}).get("recordTime") or payload.get("updateTime")
    return {"observed_at": _parse_time(observed), "temp_c": temp, "dewpoint_c": None,
            "wx_text": None}


def parse_nea(payload: dict, lat: float, lon: float) -> dict | None:
    """The reading at the station nearest the airport that has one."""
    data = payload.get("data") or {}
    stations = {s["id"]: s for s in data.get("stations") or [] if s.get("id")}
    readings = data.get("readings") or []
    if not readings:
        return None
    latest = readings[-1]
    best = None
    for item in latest.get("data") or []:
        station, value = stations.get(item.get("stationId")), _num(item.get("value"))
        loc = (station or {}).get("location") or {}
        if value is None or _num(loc.get("latitude")) is None or _num(loc.get("longitude")) is None:
            continue
        dist = _km(lat, lon, float(loc["latitude"]), float(loc["longitude"]))
        if best is None or dist < best[0]:
            best = (dist, value)
    if best is None:
        return None
    return {"observed_at": _parse_time(latest.get("timestamp")), "temp_c": best[1],
            "dewpoint_c": None, "wx_text": None}


def parse_nws(payload: dict) -> dict | None:
    props = payload.get("properties") or {}
    temp = _num((props.get("temperature") or {}).get("value"))
    dew = _num((props.get("dewpoint") or {}).get("value"))
    if temp is None and dew is None:
        return None
    present = [w.get("weather") for w in props.get("presentWeather") or [] if w.get("weather")]
    return {"observed_at": _parse_time(props.get("timestamp")), "temp_c": temp,
            "dewpoint_c": dew, "wx_text": ", ".join(present) or props.get("textDescription")}


def parse_open_meteo(payload: dict) -> dict | None:
    cur = payload.get("current") or {}
    temp, dew = _num(cur.get("temperature_2m")), _num(cur.get("dew_point_2m"))
    if temp is None and dew is None:
        return None
    code = _num(cur.get("weather_code"))
    return {"observed_at": _parse_time(cur.get("time")), "temp_c": temp, "dewpoint_c": dew,
            "wx_text": WMO_TEXT.get(int(code)) if code is not None else None}


def _symbol_text(symbol: str | None) -> str | None:
    """'lightrainshowers_day' -> 'Lightrainshowers'; the day/night suffix is dropped."""
    if not symbol:
        return None
    base = symbol.rsplit("_", 1)[0] if symbol.rsplit("_", 1)[-1] in (
        "day", "night", "polartwilight") else symbol
    return base.replace("_", " ").capitalize()


def parse_met_no(payload: dict) -> dict | None:
    series = (payload.get("properties") or {}).get("timeseries") or []
    if not series:
        return None
    first = series[0]
    details = ((first.get("data") or {}).get("instant") or {}).get("details") or {}
    temp, dew = _num(details.get("air_temperature")), _num(details.get("dew_point_temperature"))
    if temp is None and dew is None:
        return None
    symbol = (((first.get("data") or {}).get("next_1_hours") or {}).get("summary") or {}
              ).get("symbol_code")
    return {"observed_at": _parse_time(first.get("time")), "temp_c": temp, "dewpoint_c": dew,
            "wx_text": _symbol_text(symbol)}


# ---- fetchers ----

def fetch_gov(kind: str | None, icao: str, lat: float, lon: float) -> tuple[str, dict] | None:
    """(raw payload, parsed) from the airport's national service, or None."""
    if kind == "hko":
        raw = _get_json(HKO_URL, {"dataType": "rhrread", "lang": "en"})
        parsed = parse_hko(raw)
    elif kind == "nea":
        raw = _get_json(NEA_URL)
        parsed = parse_nea(raw, lat, lon)
    elif kind == "nws":
        raw = _get_json(NWS_URL.format(icao=icao),
                        headers={"User-Agent": MET_NO_USER_AGENT, "Accept": "application/geo+json"})
        parsed = parse_nws(raw)
    else:
        return None
    return (raw, parsed) if parsed else None


def fetch_open_meteo(lat: float, lon: float) -> tuple[dict, dict] | None:
    raw = _get_json(OPEN_METEO_URL, {
        "latitude": lat, "longitude": lon, "timezone": "UTC",
        "current": "temperature_2m,dew_point_2m,weather_code"})
    parsed = parse_open_meteo(raw)
    return (raw, parsed) if parsed else None


def fetch_met_no(lat: float, lon: float) -> tuple[dict, dict] | None:
    raw = _get_json(MET_NO_FORECAST_URL, {"lat": round(lat, 4), "lon": round(lon, 4)},
                    headers={"User-Agent": MET_NO_USER_AGENT})
    parsed = parse_met_no(raw)
    return (raw, parsed) if parsed else None


# ---- sunrise and sunset ----

def compute_sun(day: date, lat: float, lon: float) -> tuple[datetime | None, datetime | None]:
    """Sunrise and sunset (UTC) from the NOAA solar equations, accurate to a minute or two.
    Either is None where the sun does not rise or set that day (polar day or night)."""
    n = day.toordinal() - date(2000, 1, 1).toordinal() + 0.5  # days since J2000 noon, at 0h UT
    mean_lon = (280.46 + 0.9856474 * n) % 360
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360)
    ecliptic = math.radians(mean_lon + 1.915 * math.sin(anomaly) + 0.02 * math.sin(2 * anomaly))
    obliquity = math.radians(23.439 - 0.0000004 * n)
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic))
    # Equation of time, minutes.
    eot = 4 * math.degrees(
        math.tan(obliquity / 2) ** 2 * math.sin(2 * math.radians(mean_lon))
        - 2 * 0.0167 * math.sin(anomaly)
        + 4 * 0.0167 * math.tan(obliquity / 2) ** 2 * math.sin(anomaly)
        * math.cos(2 * math.radians(mean_lon)))
    # Sun's centre 0.833 degrees below the horizon: refraction plus its radius.
    cos_h = ((math.cos(math.radians(90.833)) - math.sin(math.radians(lat)) * math.sin(declination))
             / (math.cos(math.radians(lat)) * math.cos(declination)))
    if not -1 <= cos_h <= 1:
        return None, None
    half_day_min = 4 * math.degrees(math.acos(cos_h))
    noon_min = 720 - 4 * lon - eot
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return (midnight + timedelta(minutes=noon_min - half_day_min),
            midnight + timedelta(minutes=noon_min + half_day_min))


def parse_met_no_sun(payload: dict) -> tuple[datetime | None, datetime | None]:
    props = payload.get("properties") or {}
    return (_parse_time((props.get("sunrise") or {}).get("time")),
            _parse_time((props.get("sunset") or {}).get("time")))


def fetch_sun(day: date, lat: float, lon: float, tz: str) -> tuple[str, datetime | None, datetime | None]:
    """(source, sunrise, sunset) for the airport's local `day`: MET Norway, else computed."""
    offset = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(tz)).strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}"
    try:
        raw = _get_json(MET_NO_SUN_URL, {"lat": round(lat, 4), "lon": round(lon, 4),
                                         "date": day.isoformat(), "offset": offset},
                        headers={"User-Agent": MET_NO_USER_AGENT})
        rise, sset = parse_met_no_sun(raw)
        if rise and sset:
            return "met.no", rise, sset
        log.warning("MET Norway sun times for %s on %s incomplete; computing locally", lat, day)
    except Exception as exc:
        log.warning("MET Norway sunrise failed (%s); computing locally", exc)
    rise, sset = compute_sun(day, lat, lon)
    return "computed", rise, sset


# ---- ingest ----

def _airports(con: duckdb.DuckDBPyConnection, icaos: list[str]) -> list[dict]:
    """Coordinates and zone from the airports seed, if dbt has loaded it; the CSV otherwise."""
    from aviation.config import AIRPORTS_SEED
    import csv
    with AIRPORTS_SEED.open() as f:
        rows = {r["icao"]: r for r in csv.DictReader(f)}
    return [{"icao": i, "lat": float(rows[i]["lat"]), "lon": float(rows[i]["lon"]),
             "timezone": rows[i]["timezone"]} for i in icaos]


def ingest(con: duckdb.DuckDBPyConnection, icaos: list[str], gov: dict[str, str | None]) -> str:
    """One row per source that answered, for each airport, plus today's sun times."""
    now = warehouse.utcnow()
    rows, sun_rows, failed = [], [], []
    for ap in _airports(con, icaos):
        icao, lat, lon = ap["icao"], ap["lat"], ap["lon"]
        attempts = [
            ("gov", lambda: fetch_gov(gov.get(icao), icao, lat, lon)),
            ("open-meteo", lambda: fetch_open_meteo(lat, lon)),
            ("met.no", lambda: fetch_met_no(lat, lon)),
        ]
        for source, fetch in attempts:
            try:
                got = fetch()
            except Exception as exc:  # one provider down must not stop the others
                failed.append(f"{icao} {source}: {exc}")
                continue
            if got is None:
                continue
            raw, parsed = got
            rows.append({"icao": icao, "source": source,
                         "observed_at": parsed["observed_at"] or now, "fetched_at": now,
                         "temp_c": parsed["temp_c"], "dewpoint_c": parsed["dewpoint_c"],
                         "wx_text": parsed["wx_text"], "payload": raw})
        day = now.astimezone(ZoneInfo(ap["timezone"])).date()
        src, rise, sset = fetch_sun(day, lat, lon, ap["timezone"])
        if rise or sset:
            sun_rows.append({"icao": icao, "day": day, "source": src, "sunrise": rise,
                             "sunset": sset, "fetched_at": now})
    n = warehouse.upsert(con, "raw.wx_extra", rows, ["icao", "source", "observed_at"])
    warehouse.upsert(con, "raw.sun_times", sun_rows, ["icao", "day"])
    detail = "; ".join(failed)
    warehouse.log_run(con, "wx_extra", n, detail=detail[:500])
    return f"{n} readings, {len(sun_rows)} sun rows" + (f" ({len(failed)} failed: {detail})" if failed else "")
