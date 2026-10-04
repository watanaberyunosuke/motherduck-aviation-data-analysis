# Backlog

## NOTAM source for Australia and Singapore

**Status (2026-10-04):** WSSS, EHAM and PANC now load from FAA NOTAM Search (`src/aviation/sources/notam_faa_search.py`), the website's backend reached with curl_cffi's Chrome fingerprint. First run from a home connection: 134 records, 120 kept after dropping DoD "V" NOTAMs and Letters to Airmen; EHAM's international NOTAMs carry full ICAO text. Still to confirm: that it works from the Flight's cloud IPs, and coverage against CAAS / LVNL. An FAA NOTAM API client (`notam_faa.py`, needs `FAA_CLIENT_ID` / `FAA_CLIENT_SECRET`, untested live) is kept as the fallback. Australia stays without a source.

**Why it matters:** without NOTAMs for YSSY, YMML, YBBN and WSSS, runway closures cannot explain excess terminal-area time at those airports. `fct_arrival_weather_impact` leaves `surface_notam_in_force` and `runway_closure_in_force` null there (`has_notam_feed = false`).

**Plan**

1. Email notams@faa.gov to request access to the FAA NMS API. Ask:
   - whether it returns international NOTAMs, specifically for YSSY and WSSS;
   - whether a non-operator analytics project is eligible.
2. If approved and the API covers these airports, add an `faa_nms` client next to `notam_hk.py` and set `notam_source: faa_nms` for those airports.
3. If not, switch on SkyLink Basic ($18.59/month). The client already exists. Follow the re-enable steps in README section 1.

**Options checked on 2026-10-03** (assumes 480 calls/month: 4 airports, every 6 h)

| Option | Cost | Notes |
|---|---|---|
| FAA NMS API | Free | Official. Access by emailing notams@faa.gov. International coverage and eligibility not confirmed. |
| SkyLink on RapidAPI | $18.59/month (Basic, 5,000 requests) | No free tier. Client in `src/aviation/sources/notam_rapidapi.py`, currently switched off. Says its data comes from the FAA SWIM feed. |
| Notamify | $24.90/month + about $0.20–0.30 per call | Data source not stated. |
| AvDelphi | $0.10 per call (about $48/month) | Coverage not stated. |
| ICAO API Data Service | 100 free calls, then booster packs (prices unlisted) | NOTAMs come from FAA DINS. |
| autorouter | Free, no key | Europe only; using the data needs a Eurocontrol EAD licence (several thousand euros a year). |
| FAA DINS | Was free | `www.notams.faa.gov` did not resolve; possibly retired. |
| FAA NOTAM Search | Free website | Blocks plain scripted requests (403); works with curl_cffi's Chrome fingerprint (in use since 2026-10-04). |
| Airservices NAIPS, CAAS AIM-SG | Free accounts | Official, but web pages for pilots, not APIs. |
