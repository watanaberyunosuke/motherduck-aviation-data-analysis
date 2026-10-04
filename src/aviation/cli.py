"""Command line entry point.

    aviation ingest metar | taf | notam | notam-hk | notam-faa | notam-rapidapi | opensky | all
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from aviation import warehouse
from aviation.config import load_settings
from aviation.sources import aviationweather, notam_faa, notam_hk, notam_rapidapi, opensky


def run(source: str) -> int:
    settings = load_settings()
    con = warehouse.connect(settings.warehouse)
    failures = 0

    def attempt(name, fn):
        nonlocal failures
        try:
            result = fn()
            print(f"{name}: {result}")
        except Exception as exc:  # one source failing must not stop the others
            failures += 1
            print(f"{name}: FAILED - {exc}", file=sys.stderr)

    if source in ("metar", "all"):
        attempt("metar", lambda: aviationweather.ingest_metar(con, settings.icao_codes))
    if source in ("taf", "all"):
        attempt("taf", lambda: aviationweather.ingest_taf(con, settings.icao_codes))
    hk = settings.airports_for_notam_source("hk_cad")
    faa = settings.airports_for_notam_source("faa")
    rapid = settings.airports_for_notam_source("rapidapi")
    if source in ("notam", "notam-hk", "all") and hk:
        attempt("notam hk_cad", lambda: notam_hk.ingest(con))
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
    if source in ("opensky", "all"):
        attempt("opensky", lambda: opensky.ingest(con, settings.icao_codes, settings.opensky))

    con.close()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="aviation")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="pull a source into the raw schema")
    ingest.add_argument("source", choices=["metar", "taf", "notam", "notam-hk", "notam-faa",
                                           "notam-rapidapi", "opensky", "all"])
    args = parser.parse_args(argv)
    sys.exit(run(args.source))


if __name__ == "__main__":
    main()
