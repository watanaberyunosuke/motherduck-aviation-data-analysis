"""Command line entry point.

    aviation ingest metar | taf | notam | notam-hk | notam-faa-search | notam-faa | notam-rapidapi
                    | opensky | aerodatabox | all
    aviation backfill weather [--days N]
    aviation backfill flights [--opensky-calls N] [--aerodatabox-calls N]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from aviation import warehouse
from aviation.config import load_settings
from aviation.sources import (
    aerodatabox, aviationweather, notam_faa, notam_faa_search, notam_hk, notam_rapidapi, opensky,
)


def run(source: str, days: int | None = None, opensky_calls: int | None = None,
        aerodatabox_calls: int | None = None) -> int:
    """Ingest one source (or `all`), or run a backfill (`backfill-weather`,
    `backfill-flights`). The call budgets override max_backfill_calls_per_run."""
    settings = load_settings()
    con = warehouse.connect(settings.warehouse)
    failures = 0

    def attempt(name, fn) -> bool:
        nonlocal failures
        try:
            result = fn()
            print(f"{name}: {result}")
            return True
        except Exception as exc:  # one source failing must not stop the others
            failures += 1
            print(f"{name}: FAILED - {exc}", file=sys.stderr)
            return False

    # Weather is fetched twice a day, so each run covers the gap since the last one.
    lookback = int(settings.weather.get("lookback_hours", 26))
    if source in ("metar", "all"):
        attempt("metar", lambda: aviationweather.ingest_metar(con, settings.icao_codes, lookback))
    if source in ("taf", "all"):
        attempt("taf", lambda: aviationweather.ingest_taf(con, settings.icao_codes, lookback))
    hk = settings.airports_for_notam_source("hk_cad")
    faa_search = settings.airports_for_notam_source("faa_search")
    faa = settings.airports_for_notam_source("faa")
    rapid = settings.airports_for_notam_source("rapidapi")
    if source in ("notam", "notam-hk", "all") and hk:
        attempt("notam hk_cad", lambda: notam_hk.ingest(con))
    if source in ("notam", "notam-faa-search", "all") and faa_search:
        attempt("notam faa_search", lambda: notam_faa_search.ingest(con, faa_search))
    if source in ("notam", "notam-faa", "all") and faa:
        # The FAA key is optional: without it those airports read as having no NOTAM feed
        # (the marts only count a source once it has loaded), so skip rather than fail.
        missing = [k for k in notam_faa.CREDENTIALS if not os.environ.get(k, "").strip()]
        if missing:
            print(f"notam faa: skipped - {' / '.join(missing)} not set")
        else:
            attempt("notam faa", lambda: notam_faa.ingest(con, faa))
    if source in ("notam", "notam-rapidapi", "all") and rapid:
        attempt("notam rapidapi", lambda: notam_rapidapi.ingest(con, rapid))
    # OpenSky first: it is preferred, and AeroDataBox then fills the days it came back
    # empty for, or everything it has not loaded if it failed. (AeroDataBox run on its own
    # assumes OpenSky works.) OpenSky's tracks for AeroDataBox's flights follow next run.
    flights = ("all", "backfill-flights")
    adb_cfg = settings.aerodatabox
    if aerodatabox_calls is not None:
        adb_cfg = {**adb_cfg, "max_backfill_calls_per_run": aerodatabox_calls}
    os_cfg = settings.opensky
    if opensky_calls is not None:
        os_cfg = {**os_cfg, "max_backfill_calls_per_run": opensky_calls}
    opensky_ok = True
    if source in ("opensky", *flights):
        opensky_ok = attempt("opensky", lambda: opensky.ingest(con, settings.icao_codes, os_cfg))
    if source in ("aerodatabox", *flights):
        missing = [k for k in aerodatabox.CREDENTIALS if not os.environ.get(k, "").strip()]
        if missing:
            print(f"aerodatabox: skipped - {' / '.join(missing)} not set; OpenSky covers flights")
        else:
            attempt("aerodatabox",
                    lambda: aerodatabox.ingest(con, settings.timezones, adb_cfg, opensky_ok))
    if source == "backfill-weather":
        days = days or int(settings.weather.get("backfill_days", 30))
        attempt("weather backfill",
                lambda: aviationweather.backfill(con, settings.icao_codes, days))

    con.close()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="aviation")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="pull a source into the raw schema")
    ingest.add_argument("source", choices=["metar", "taf", "notam", "notam-hk", "notam-faa-search",
                                           "notam-faa", "notam-rapidapi", "opensky",
                                           "aerodatabox", "all"])
    backfill = sub.add_parser("backfill", help="load history (flights backfill within ingest)")
    backfill.add_argument("what", choices=["weather", "flights"])
    backfill.add_argument("--days", type=int, help="weather: default weather.backfill_days")
    backfill.add_argument("--opensky-calls", type=int,
                          help="flights: OpenSky backfill calls this run (the credit floor "
                               "still applies); default opensky.max_backfill_calls_per_run")
    backfill.add_argument("--aerodatabox-calls", type=int,
                          help="flights: AeroDataBox backfill calls this run (paid units); "
                               "default aerodatabox.max_backfill_calls_per_run")
    args = parser.parse_args(argv)
    if args.command == "backfill":
        sys.exit(run(f"backfill-{args.what}", args.days, args.opensky_calls,
                     args.aerodatabox_calls))
    sys.exit(run(args.source))


if __name__ == "__main__":
    main()
