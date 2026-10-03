"""Regenerate the ICAO <-> IATA code seeds in dbt/seeds/.

    python scripts/build_reference_seeds.py

OpenSky reports airports and callsigns in ICAO form (YSSY, QFA627). The dashboards show
IATA (SYD, QF627), so dbt joins against two lookup seeds built here:

  airport_codes.csv  every airport with both an ICAO and an IATA code, from OurAirports
                     (public domain, https://ourairports.com/data/).
  airlines.csv       active airlines with both codes, from OpenFlights (ODbL,
                     https://openflights.org/data). OpenFlights is not maintained, so a
                     few codes are pinned in AIRLINE_OVERRIDES below.

Run it by hand when codes need refreshing; the output is committed, so dbt never
downloads anything.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import requests

SEEDS = Path(__file__).parents[1] / "dbt" / "seeds"
OURAIRPORTS = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OPENFLIGHTS = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"

AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport"}

# ICAO designator -> (IATA designator, name). Wins over OpenFlights: covers carriers it
# lists under more than one IATA code, and current regional operators it lacks.
AIRLINE_OVERRIDES = {
    "TGW": ("TR", "Scoot"),
    "QLK": ("QF", "QantasLink"),
    "NWK": ("QF", "Network Aviation (QantasLink)"),
    "RXA": ("ZL", "Regional Express"),
    "HKE": ("UO", "HK Express"),
    "CRK": ("HX", "Hong Kong Airlines"),
    "GBA": ("HB", "Greater Bay Airlines"),
    "BTK": ("ID", "Batik Air"),
    "MXD": ("OD", "Batik Air Malaysia"),
    "SJX": ("JX", "Starlux Airlines"),
    "VJC": ("VJ", "VietJet Air"),
    "NZM": ("NZ", "Air New Zealand Link"),
    "FDX": ("FX", "FedEx Express"),
    "UPS": ("5X", "UPS Airlines"),
    "FJI": ("FJ", "Fiji Airways"),
}


def airport_rows() -> list[dict]:
    text = requests.get(OURAIRPORTS, timeout=60).text
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        icao, iata = (r["icao_code"] or "").strip(), (r["iata_code"] or "").strip()
        if r["type"] in AIRPORT_TYPES and len(icao) == 4 and len(iata) == 3:
            rows.append({"icao": icao, "iata": iata, "name": _clean(r["name"]),
                         "country": r["iso_country"],
                         "lat": round(float(r["latitude_deg"]), 4),
                         "lon": round(float(r["longitude_deg"]), 4)})
    rows.sort(key=lambda r: r["icao"])
    icaos = [r["icao"] for r in rows]
    assert len(icaos) == len(set(icaos)), "OurAirports has duplicate ICAO codes"
    return rows


def airline_rows() -> list[dict]:
    text = requests.get(OPENFLIGHTS, timeout=60).text
    found: dict[str, set[tuple[str, str]]] = {}
    for _id, name, _alias, iata, icao, _callsign, _country, active in csv.reader(io.StringIO(text)):
        valid_iata = len(iata) == 2 and iata.isalnum() and iata == iata.upper()
        valid_icao = len(icao) == 3 and icao.isalpha() and icao == icao.upper()
        if active == "Y" and valid_iata and valid_icao:
            found.setdefault(icao, set()).add((iata, _clean(name)))
    rows = {icao: {"icao": icao, "iata": iata, "name": name}
            for icao, codes in found.items()
            # Ambiguous designators are left out unless overridden: a wrong flight number
            # is worse than showing the raw callsign.
            if len({c[0] for c in codes}) == 1
            for iata, name in [min(codes)]}
    for icao, (iata, name) in AIRLINE_OVERRIDES.items():
        rows[icao] = {"icao": icao, "iata": iata, "name": name}
    return sorted(rows.values(), key=lambda r: r["icao"])


def _clean(name: str) -> str:
    # dbt-duckdb's seed loader rejects "" escapes inside quoted fields.
    return name.replace('"', "").strip()


def write(name: str, rows: list[dict]) -> None:
    path = SEEDS / name
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"{path}: {len(rows)} rows")


if __name__ == "__main__":
    write("airport_codes.csv", airport_rows())
    write("airlines.csv", airline_rows())
