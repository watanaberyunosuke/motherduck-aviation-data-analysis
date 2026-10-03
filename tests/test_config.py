import csv
from pathlib import Path

from aviation.config import load_settings

SEED = Path(__file__).parents[1] / "dbt" / "seeds" / "airports.csv"


def test_seed_notam_source_matches_config():
    """The dbt seed decides which arrivals get NOTAM flags; it must agree with ingest."""
    seed = {r["icao"]: r["notam_source"] or None for r in csv.DictReader(SEED.open())}
    config = {a.icao: a.notam_source for a in load_settings().airports}
    assert seed == config


def test_seed_iata_matches_airport_codes():
    """In-scope airports must carry the same IATA code as the worldwide lookup."""
    codes = {r["icao"]: r["iata"] for r in csv.DictReader((SEED.parent / "airport_codes.csv").open())}
    seed = {r["icao"]: r["iata"] for r in csv.DictReader(SEED.open())}
    assert seed == {icao: codes[icao] for icao in seed}
