# Aviation data analysis

How much does weather and runway availability cost arriving flights at Sydney, Melbourne, Brisbane, Singapore, Hong Kong, Amsterdam and Anchorage?

- Ingests METAR/TAF weather, NOTAMs (every airport except the Australian ones), scheduled and actual flight times, and ADS-B flight paths for the last 30 days on a schedule into DuckDB (local) or MotherDuck (scheduled runs).
- Transforms with dbt into marts that line up each arrival with the weather and NOTAMs in force when it landed.
- Measures **excess terminal-area time**, the minutes an arrival spends within 50 NM of its destination beyond that airport's rolling median. This is the weather-sensitive part of a flight: holding, vectoring and go-arounds.
- Scheduled by a [MotherDuck Flight](https://motherduck.com/docs/concepts/flights/) and visualised in a [MotherDuck Dive](https://motherduck.com/docs/key-tasks/dives/). The same Dive is also served on Vercel, running in the browser on DuckDB-WASM.
- Shows airports and flights with IATA codes (SYD, QF627). The warehouse stores the ICAO forms OpenSky reports (YSSY, QFA627) alongside.
- Inspired by [colin-k-rogers/formula-1-data-analysis](https://github.com/colin-k-rogers/formula-1-data-analysis): scheduled ingest, then dbt, then dashboard.

## 1. Data sources

| Data | Source | Airports | Access | Cadence |
|---|---|---|---|---|
| METAR, TAF | [Aviation Weather Center Data API](https://aviationweather.gov/data/api/) (preferred) | All seven | Free, no key | Hourly; last 30 days |
| METAR history, METAR fallback | [Iowa Environmental Mesonet archive](https://mesonet.agron.iastate.edu/request/download.phtml) | All seven | Free, no key | Only if AWC fails, or for history beyond 30 days |
| NOTAMs (Hong Kong) | [HK CAD NOTAM website](https://www.notam.ais.gov.hk/), JSON at `/data` | VHHH, VHHK FIR | Free, official, no key | Every 3 h |
| NOTAMs (US and international) | [FAA NOTAM Search](https://notams.aim.faa.gov/notamSearch/nsapp.html) (the website's backend) | WSSS, EHAM, PANC | Free, no key; unofficial interface | Every 3 h |
| NOTAMs (AU) | None at present | YSSY, YMML, YBBN | See below | Not ingested |
| Flights (preferred), flight paths | [OpenSky Network REST API](https://openskynetwork.github.io/opensky-api/rest.html) | All seven | Free account, OAuth2 client | Hourly check; last 30 days |
| Flights with schedules (fallback) | [AeroDataBox](https://aerodatabox.com) airport boards | All seven | Paid (RapidAPI or API.market key) | Hourly check, only for days OpenSky missed; last 30 days |

**Weather.** The [Aviation Weather Center](https://aviationweather.gov/help/data/) is the primary source. Its API serves only the last 30 days, at most 100 requests a minute and 400 results a request. METARs older than that come from the IEM archive (`src/aviation/sources/iem.py`), reshaped into the AWC's JSON (with `"source": "iem"`) so `stg_metar` reads both alike; `stg_metar.source` says which. IEM rows never replace AWC rows, and IEM also stands in for the scheduled METAR fetch if the AWC call fails. Compared on the same reports, the two agree field for field outside the US; for PANC the IEM altimeter can differ by 0.1 hPa. Weather is fetched every hour, each run covering the last `weather.lookback_hours` (26) so delayed or dropped runs leave no gap. `aviation backfill weather` loads `weather.backfill_days` (30) from AWC; a longer window would take older METARs from IEM. There is no free archive of non-US TAFs.

**Flights.** OpenSky is the preferred source: it is free and the only source of flight paths. [AeroDataBox](https://doc.aerodatabox.com) is the fallback. Each run fetches OpenSky first, then AeroDataBox for the airport-days OpenSky returned no flights for (a gap in its receivers or an outage). If OpenSky fails during a run, AeroDataBox fills every day OpenSky has not loaded. While OpenSky works, days it has not reached yet are left for its later runs, which are free. `stg_flights` uses OpenSky for every (airport, direction, UTC day) slot it has flights for and AeroDataBox for the rest (`raw.flight_slots` records which source loaded which slot and how many flights it returned). OpenSky also fetches slots AeroDataBox loaded earlier and takes them over. AeroDataBox knows the timetable, so `fct_arrivals` and `fct_departures` carry `scheduled_at` and `delay_minutes` for its flights; OpenSky's flights have no schedule. Tracks are fetched for AeroDataBox flights too, by their Mode-S address. AeroDataBox's airport endpoint is a TIER 2 call covering at most 12 hours, so an airport-day is 2 calls. If OpenSky were down for good, all seven airports would take 14 calls a day (about 420 a month, plus about 420 once to fill 30 days), which the free tier does not cover. Set `AERODATABOX_KEY` and `aerodatabox.provider` (`rapidapi` or `apimarket`) to switch it on; without a key it is skipped. Flights cover the last `backfill_days` (30, `config/airports.yml`). Each run fetches only the (airport, direction, day) slots not yet loaded, newest first, so the hourly run spends OpenSky credits and AeroDataBox units only on gaps; AeroDataBox stays within `max_backfill_calls_per_run`. A day is loaded once it is complete, from 06 UTC the next day (after OpenSky's nightly batch). Each OpenSky airport-day costs 30 of its roughly 4,000 daily credits, so filling 30 days for seven airports takes about four days of runs; after that each new day is 14 calls.

FAA NOTAM Search carries US NOTAMs and the international NOTAMs the FAA receives through the ICAO exchange, which is how Singapore and Amsterdam are covered. It is a copy, not the issuing authority (CAAS, LVNL), so spot-check it against the official source before relying on completeness. `src/aviation/sources/notam_faa_search.py` calls the search form's own JSON backend, which is not a published API. Akamai refuses plain HTTP clients there (403 even for the HTML page), so the client uses [curl_cffi](https://github.com/lexiforest/curl_cffi) to present Chrome's TLS fingerprint, loads the page for its cookies, then pages through the results 30 at a time with a pause between requests. That can stop working without notice. It drops the US DoD "V" series NOTAMs (republished foreign procedure changes) and FAA Letters to Airmen. US domestic NOTAMs (`!ANC ...`) have no Q-line, so they get no category and cannot flag a runway closure.

The fallback is the official [FAA NOTAM API](https://api.faa.gov/s/) (`notam_faa.py`, `notam_source: faa`), which needs `FAA_CLIENT_ID` / `FAA_CLIENT_SECRET` and is untested against the live API. An airport whose source has never loaded shows no NOTAM feed rather than zero NOTAMs: the marts only treat a source as a feed once it has loaded, and only for arrivals after it first did.

Australia (Airservices NAIPS) publishes live NOTAMs only to registered users. Its NOTAMs should also reach the FAA; switch the Australian airports to `notam_source: faa_search` once a spot-check against NAIPS shows the FAA copy is complete. A client for the paid [SkyLink NOTAM API](https://github.com/SkyLink-API/notam-api) on RapidAPI (Basic plan $18.59/month for 5,000 requests; no free tier) is kept in `src/aviation/sources/notam_rapidapi.py` but is switched off. To re-enable it, set `notam_source: rapidapi` for those airports in `config/airports.yml` and `dbt/seeds/airports.csv`, add `RAPIDAPI_KEY`, and restore the 6-hourly schedule in `ingest.yml`.

## 2. Architecture

```
                         src/aviation            dbt/models                     ┌─> Vercel site (web/ + api/, DuckDB-WASM)
aviationweather.gov ─┐
IEM (METAR archive) ─┤
notam.ais.gov.hk ────┼─> ingest (Python) ──> raw.* ──> staging.* ──> marts.* ──┤
OpenSky Network ─────┤   idempotent upserts,        views          tables      └─> MotherDuck Dive (dives/)
AeroDataBox ─────────┘
                         full JSON payload kept
        └── weather, NOTAMs and flights: GitHub runner, hourly; Flight aviation_pipeline on demand ──┘
```

- **Raw layer** (`src/aviation/warehouse.py`): one table per source, keyed on the natural key, with the full payload as JSON. Re-running any fetch overwrites rather than duplicates.
- **NOTAM parsing** (`src/aviation/parsing/notam.py`): ICAO items A to G and the Q-line are parsed at load time. Items are read in their fixed order so text such as `TWY C)` is not mistaken for item C. NOTAMs record `first_seen_at` and `last_seen_at`, so a NOTAM dropping out of a feed (cancelled or expired) is visible.
- **Marts** (`dbt/models/marts`):

| Model | Grain | Purpose |
|---|---|---|
| `fct_airport_weather_hourly` | airport, UTC hour | Latest METAR with IFR, gust and thunderstorm flags |
| `fct_notams` | NOTAM per source | Category from Q-code, runway-closure and in-force flags |
| `fct_daily_airport_movements` | airport, UTC day | Observed arrivals and departures |
| `fct_arrivals` | observed arrival | Every arrival at an in-scope airport, from any origin, with the flight category at landing and terminal metrics where the flight was tracked |
| `fct_departures` | observed departure | Every departure from an in-scope airport, to any destination, with the flight category at take-off and time to leave the 50 NM terminal area against the airport's rolling median, where the flight was tracked |
| `fct_airport_conditions` | airport | Time zone and the latest METAR (decoded and raw), TAF and NOTAM count |
| `fct_terminal_tracks` | track point | Flight-path points within 250 km of an airport (arrival and departure ends), one per 30 s, for the map |
| `fct_flight_track_metrics` | tracked flight | Path length, route inefficiency, terminal-area time, coverage checks |
| `fct_arrival_weather_impact` | arrival | **Primary table.** Excess terminal time plus weather and NOTAMs at arrival. NOTAM flags are null where `has_notam_feed` is false |

- **ICAO and IATA codes**: OpenSky reports airports and callsigns in ICAO form. Every mart that names an airport also carries its IATA code (`arrival_iata`, `departure_iata`, `iata`, `location_iata`), and flights carry both `callsign` (QFA627) and `flight_number_iata` (QF627). The lookups are two seeds generated by `scripts/build_reference_seeds.py`: `airport_codes` (every airport with both codes, from [OurAirports](https://ourairports.com/data/), public domain) and `airlines` (from [OpenFlights](https://openflights.org/data), ODbL, with a few current codes pinned in the script). Only callsigns of the form designator + digits become flight numbers; alphanumeric ATC callsigns (QLK10D), registrations and unlisted operators keep `flight_number_iata` null and are shown by callsign. Re-run the script to refresh the codes.

## 3. Why terminal-area time, not delay

OpenSky observes aircraft; it has no timetables. Without scheduled times, schedule delay cannot be calculated. Terminal-area time is observable from the flight path, responds directly to weather and runway capacity, and is comparable across airports once each airport is benchmarked against its own median.

AeroDataBox supplies that schedule for the days it fills in for OpenSky: its flights carry `scheduled_at` and `delay_minutes` in `fct_arrivals` and `fct_departures`. Terminal-area time stays the weather measure, because schedule delay also reflects the departure end, the airline and padding in the timetable.

## 4. Setup

```bash
conda create -n aviation python=3.11 -y && conda activate aviation
pip install -e ".[dev]"
cp .env.example .env        # fill in OPENSKY_* (and FAA_* for FAA NOTAMs)

make ingest-weather          # works with no credentials
aviation ingest notam-hk     # works with no credentials
aviation ingest notam-faa-search   # works with no credentials
make ingest-all              # needs OpenSky credentials
make transform               # dbt seed + run + test
make test                    # pytest, including a full dbt build on a temp database
```

Credentials:

- **OpenSky**: create a free account, then Account > API client. New accounts must use OAuth2 client credentials.
- **FAA NOTAM API** (optional fallback, unused at present): register at [api.faa.gov](https://api.faa.gov/s/) and request access to the NOTAM API; it issues a client id and secret. Set `FAA_CLIENT_ID` and `FAA_CLIENT_SECRET`.
- **MotherDuck** (scheduled runs only): set `WAREHOUSE=md:aviation` and `MOTHERDUCK_TOKEN`. Check MotherDuck's current free-tier limits before relying on it.

## 5. Scheduling and dashboards

### Flight: `aviation_pipeline`

A [MotherDuck Flight](https://motherduck.com/docs/concepts/flights/) runs weather and NOTAM ingest, then `dbt build`, on demand: from the `ingest` workflow with `runner: flight` (`scripts/run_flight.py`), and once after each deploy as a smoke test. MotherDuck only schedules Flights on paid plans and this account is on the free plan, so the Flight is published unscheduled and GitHub starts it with `md_run_flight`, waits for the run and prints its logs; a failed run fails the workflow. Each run downloads the commit it is pinned to from GitHub, runs METAR, TAF, HK and FAA NOTAM Search ingest, then `dbt build`. If the plan's 30 daily Flight minutes are spent, `scripts/run_flight.py` exits 75 and the workflow runs the same sources on the GitHub runner instead; the deploy job's Flight smoke test is skipped with a warning. Ingest and dbt run in one process, so they never write to the warehouse at the same time.

The Flight used to run the twice-daily weather and NOTAM load. Once the marts grew, its `dbt build` took up to half an hour (`fct_arrivals` and `fct_departures`), more than the daily Flight minutes, so the schedule now runs everything on the GitHub runner, with one `dbt build` an hour.

| Schedule (UTC) | Where | What |
|---|---|---|
| Every hour, at :17 | GitHub runner | METAR and TAF (last 26 h), HK and FAA NOTAMs; `aviation backfill flights`: missing days of the last 30 (OpenSky, then AeroDataBox for days OpenSky missed if its key is set), tracks for the newest day; `dbt build` |

GitHub runs scheduled workflows late or not at all when it is busy, most often on the hour, so the run is at :17 and the 26-hour weather lookback covers gaps of several hours. Most hourly runs find every flight day loaded and only add tracks; a new day is fetched once it is complete (06 UTC). OpenSky has timed out from the Flight's servers but not from GitHub's. `SOURCES: 'opensky aerodatabox'` still runs flights in the Flight on demand.

The source is `flights/aviation_pipeline/main.py`. OpenSky credentials come from a Flight secret named `opensky`, FAA NOTAM API credentials (for the unused `faa` fallback) from an optional one named `faa`, and the AeroDataBox key from an optional one named `aerodatabox`; the deploy creates them from the GitHub secrets (`AERODATABOX_KEY` for the last). To run it now, or with other sources:

```sql
-- flight_id from: select flight_id from md_list_flights() where flight_name = 'aviation_pipeline'
select * from md_run_flight(flight_id := '<id>', config := MAP {'SOURCES': 'opensky'});
select * from md_list_flight_runs(flight_id := '<id>') order by run_number desc limit 5;
select * from md_get_flight_logs(flight_id := '<id>', run_number := <n>);
```

`SOURCES` takes space-separated sources (`metar taf notam-hk notam-faa-search opensky aerodatabox`), or `none` for dbt only. Run on its own, `aerodatabox` fills only the days OpenSky returned empty.

### Flight: `initial_load`

The history load runs as its own Flight, `flights/initial_load/main.py`, inside MotherDuck: from a laptop every write is a round trip to MotherDuck, which made the load take hours. It is published with the pipeline but never scheduled. It runs `aviation backfill weather` (30 days of METARs and TAFs), then `aviation backfill flights` (the missing days of the last 30: OpenSky with as many calls as the day's credits allow, then AeroDataBox for the days OpenSky missed if its key is set), then `dbt build`. A Flight run is capped at an hour, so the work can be split with config, and every step is safe to re-run:

```bash
python scripts/run_flight.py --flight initial_load --timeout-minutes 60                          # everything
python scripts/run_flight.py --flight initial_load --config STEPS=weather --timeout-minutes 60   # weather only
python scripts/run_flight.py --flight initial_load --config STEPS=flights                        # another day's OpenSky credits
```

Config: `STEPS` (`weather`, `flights`), `DAYS` (weather history), `OPENSKY_CALLS` (default 1000; the credit floor stops it first), `AERODATABOX_CALLS` (paid units for the fallback; default from config) and `DBT` (`false` to skip). On the free plan only one Flight runs at a time, so do not start both Flights together. OpenSky allows about a week of all seven airports a day, so the hourly flights run on GitHub fills the rest of the 30 days. The same loads run on a GitHub runner, using no Flight minutes, from the `ingest` workflow with source `backfill-weather` or `backfill-flights` and `runner: github`. `scripts/deploy_motherduck.py --only flight --flight initial_load` publishes just this Flight (for example from a branch) without repointing the scheduled pipeline.

From GitHub, run the `ingest` workflow manually and pick the sources. Choosing `runner: github` runs ingest and dbt on the GitHub runner instead of the Flight, as a fallback if the Flight is unavailable.

### Agent sessions (Entire)

The repo is set up for [Entire](https://entire.io), which links AI agent sessions to the commits they produce. `.entire/settings.json` enables it for Claude Code (`.claude/settings.json`) and Cursor (`.cursor/hooks.json`). Install the CLI (`curl -fsSL https://entire.io/install.sh | bash` or Homebrew), then run `entire enable` once per clone to add its git hooks; `entire status` shows the state and `entire checkpoint` / `entire search` browse past sessions.

This repository is public, so `push_sessions` is `false`: checkpoints are stored as local git refs and never pushed. To share them, push to a separate private repository (`entire enable --checkpoint-remote github:<owner>/<private-repo>`) rather than to this one. Telemetry is off. The agent hooks do nothing if the Entire CLI is not installed.

### Dive: Airport conditions

`dives/airport_conditions/index.tsx` is a [MotherDuck Dive](https://motherduck.com/docs/key-tasks/dives/), a React component that MotherDuck hosts and that queries `aviation.marts` live. It opens with clocks for UTC, the selected airport and Melbourne, and 7-day weather shares for every airport, then drills into one airport (HKG by default):

- current conditions: the latest METAR decoded and raw, the latest TAF, and the number of NOTAMs in force;
- NOTAMs in force: the full text of every NOTAM for the airport whose validity covers now, newest first, with its Q-code category, validity and any schedule, filterable by category;
- an airspace map: live aircraft within 500 NM, with this airport's inbound and outbound flights coloured by delay status (red / amber / green), observed arrival and departure paths of tracked flights over the last 3 days (which trace the procedures in use), the 50 NM terminal area and the wind. Official SID/STAR geometry is not drawn: no free procedure data covers these airports;
- en route flights: airborne inbound and outbound flights within 500 NM with a red / amber / green delay status, distance, altitude, speed, usual time and a rough ETA. Live ADS-B carries no origin, destination or schedule, so both direction and "usual time" come from the callsign's last 30 days at the airport (`fct_arrivals` / `fct_departures`; usual time is the median local time of day, wrapped around midnight). Delay compares the ETA (inbound: ground speed to the 50 NM ring plus the airport's median terminal time) or estimated take-off (outbound, the same backwards) with that usual time: green under 15 min late, amber 15-44, red 45 or more. It is a proxy for schedule delay, not a substitute;
- arrival statistics, 72 hours of wind and flight category, daily movements, the weather penalty, every arrival observed on the latest local day (from any origin) and the slowest arrivals;
- departure statistics (time to leave the terminal area against the airport's median) and every departure on the latest local day.

Flight and chart times are the airport's local time (`reference.airports.timezone`); METAR and TAF times stay in UTC. The selected airport is kept in the URL as its IATA code (`?airport=SYD`; ICAO links still work), so a link opens the same view.

The map is plain SVG over Esri's light grey basemap tiles, so it needs no map library. Live positions come from `/api/live/<icao>` on the Vercel site, edge-cached for 2 minutes so every viewer shares one call; neither source allows browser requests from other sites, hence the proxy. It tries OpenSky's `/states/all` for a 16.6 x 16.6 degree box first (3 credits; anonymous unless `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` are set in the Vercel project), then [adsb.lol](https://adsb.lol) (open data, ODbL, no key) within 500 NM. OpenSky timed out from Vercel's servers in testing, so expect adsb.lol in production. The response names the source that answered and why any earlier one failed, and the page credits the source. Inside MotherDuck the Dive calls the production Vercel URL; if MotherDuck blocks that request, the map still shows paths and weather and says live positions are unavailable.

To preview edits with hot reload, install the [MotherDuck CLI](https://motherduck.com/docs/sql-reference/motherduck-cli/) and run `motherduck dive watch dives/airport_conditions`. Only `index.tsx` and `dive.metadata.json` (title, description) are deployed.

The Dive queries `md:aviation` directly, so viewers need access to that database. To share the Dive with someone else in your organisation, share the database with them first.

### Vercel site

The Vercel site is the same Dive, for viewers without MotherDuck access. `web/` bundles `dives/airport_conditions/index.tsx` unchanged with Vite and swaps its `@motherduck/react-sql-query` import for `web/src/dive-runtime.ts`, which runs the same SQL in the browser on [DuckDB-WASM](https://duckdb.org/docs/api/wasm/overview). The WASM build is served from the deployment; DuckDB's ICU extension is fetched from DuckDB's extension CDN on first load.

The MotherDuck token never reaches the browser. The page fetches each table its SQL names from `api/index.py` (`/api/tables/<schema>.<table>`), which exports it from MotherDuck as Parquet, trimmed to the last 31 days where the Dive needs no more, and Vercel's edge caches it for 10 minutes (browsers revalidate on every load). Only the tables in its allow-list can be fetched. Each export must stay under Vercel's 4.5 MB response limit; `fct_arrivals` is about 30 KB per 1,000 rows.

`/api/snapshot/<icao>` serves the same marts as JSON for one airport, for clients without DuckDB: the airport list, current conditions, 24 hours of hourly weather, NOTAMs in force, median terminal times and the 30-day callsign history the Dive's boards are predicted from. `/api/tracks/<icao>` adds the observed arrival and departure paths of the last 3 days, one coordinate list per track, for a map. Both are edge-cached like the tables. The iOS ramp app (a separate repository, `motherduck_aviation_data_ios`) reads them together with `/api/live/<icao>`.

Vercel builds from `vercel.json`: `yarn build` in `web/` for the static site, and `api/index.py` as a Python function with its own `api/requirements.txt` (duckdb, fastapi). The project needs `MOTHERDUCK_TOKEN` set in its environment variables. To run it locally against the local warehouse:

```bash
WAREHOUSE=data/aviation.duckdb uvicorn api.index:app --port 8000   # pip install uvicorn
cd web && yarn install --frozen-lockfile && yarn dev               # proxies /api to :8000
```

### Deploying

`scripts/deploy_motherduck.py` (or `make deploy-motherduck`) publishes both. It matches the Flight by name and the Dive by title, creates them if missing and updates them otherwise; every update is a new version in MotherDuck. The two are published independently, so a Flight failure does not stop the Dive. Run locally, it refuses a commit that is not yet on GitHub, because the Flight would fail to download it. Pass `--only flight` or `--only dive` to publish one. In CI, a push to main deploys only what it changed: dbt seed + build when `dbt/` (or the pinned dbt version in `pyproject.toml` / `uv.lock`) changed, the Flight when `flights/`, `src/`, `dbt/` or `config/` changed (the Flight runs those from its pinned commit), the Dive when `dives/` changed; a manual run does all three.

`ci.yml` is the CI/CD pipeline:

- **test**: on every push and pull request, runs `pytest` (including a full dbt build on a temporary DuckDB file) on Python 3.11 and 3.14.
- **deploy**: on pushes to `main`, after tests pass, rebuilds seeds and runs `dbt build` against MotherDuck (`md:aviation`), creating the database if it does not exist. It then points the Flight at the new commit and publishes the Dive. It uses the `production` environment, so you can add required reviewers under Settings > Environments. It needs the `MOTHERDUCK_TOKEN`, `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET` secrets, and optionally `FAA_CLIENT_ID` and `FAA_CLIENT_SECRET`.

Deploy and the ingest workflow share a concurrency group, so they never write to the warehouse at the same time. Because the ingest job waits for the Flight run, the group covers the Flight too, unless you start a run directly with `md_run_flight`. Both install dependencies from `uv.lock`; after changing dependencies in `pyproject.toml`, run `uv lock` and commit the lockfile.

## 6. Known limitations

- **No NOTAMs for Australia or Singapore.** Only Hong Kong has a NOTAM feed, so runway closures cannot explain excess terminal time at YSSY, YMML, YBBN or WSSS. For those arrivals `surface_notam_in_force` and `runway_closure_in_force` are null (unknown), not false. Filter on `has_notam_feed` before comparing NOTAM effects across airports.
- **SkyLink client is untested against live data.** If it is re-enabled, check on the first real call whether YSSY and WSSS NOTAMs arrive in ICAO format (the provider's example is FAA domestic format, with no Q-line) and spot-check completeness against NAIPS / AIM-SG. A response shape change raises `SchemaMismatch` rather than loading bad rows.
- **OpenSky coverage in Australia and Asia is thinner than in Europe and North America.** Terminal time is only trusted when `fct_flight_track_metrics.has_arrival_coverage` is true: the track is seen outside the destination's terminal area before entering it and within 30 km of the runway at the end. The impact mart uses only those flights, from any origin. `has_full_coverage` (seen near both runways) still gates path length and route inefficiency. Expect a lower share of usable flights than in Europe.
- **Few arrivals have terminal metrics.** `fct_arrivals` lists every observed arrival, but only flights whose track was fetched have terminal time, and the tracks quota covers a small share of a day's arrivals (`track_arrivals_only` in `config/airports.yml` limits tracking to arrivals at in-scope airports).
- **30 days of history.** Flights and weather are kept to the last 30 days (`backfill_days`). Filling 30 days of flights from OpenSky takes about four days of credits; METARs loaded earlier from IEM (back to October 2023) stay in `raw.metar`.
- **AeroDataBox untested against the live API.** The client follows the published OpenAPI spec; no key was available when it was written. Check the first run's `raw.ingest_log` row and a payload. Times without an actual (runway or revised) value fall back to the schedule and are flagged `*_time_is_scheduled`, with no delay.
- **OpenSky quotas.** Tracks are limited to the last 30 days and the endpoint is marked experimental. The client reads `X-Rate-Limit-Remaining`, stops below `min_credits_remaining`, and caps calls at `max_tracks_per_run` (`config/airports.yml`). A track costs several credits (4 to 30 seen), so flights are tracked newest first. Each run's `raw.ingest_log` row records the HTTP status counts per endpoint (`http={'tracks': {200: ..., 404: ...}}`), so a run that stores no tracks shows whether OpenSky returned 404s or ran out of credits.
- **NOTAM schedules.** A NOTAM with a D) schedule is only active in its sub-windows. `has_schedule` flags these; the in-force checks currently treat the whole B) to C) window as active, which overstates them.
- **HK feed terms.** The CAD site describes its data as informational. Read its Important Notices before using the data beyond analysis.

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Environment variable X is not set` | Copy `.env.example` to `.env`, or add the GitHub secret. Other sources still run. |
| OpenSky `403 You cannot access historical flights` | Missing or invalid OAuth client. Check `OPENSKY_CLIENT_ID` / `SECRET`. |
| OpenSky run logs `stopped early` | Daily credit floor reached, or the backfill budget spent (normal). Lower `max_tracks_per_run` or run later; flights already landed are kept. |
| `aerodatabox: skipped - AERODATABOX_KEY not set` | Expected until a key is added; OpenSky covers flights. |
| AeroDataBox `429` / `stopped_early` with a budget message | Monthly units or the per-run budget spent. Lower `aerodatabox.max_backfill_calls_per_run` to fit the plan. |
| `notam faa_search: FAILED - NOTAM Search returned 403` (`Blocked`) | Akamai refused the client. Try upgrading `curl_cffi` (newer Chrome fingerprints); if it keeps failing, especially from the Flight's cloud IPs, switch those airports to `notam_source: faa` with an FAA API key. |
| `SchemaMismatch` from FAA NOTAM Search or the FAA API | Response shape changed. Inspect one raw response (`raw.notam.payload`) and update `notam_faa_search.rows` / `notam_faa.rows`. |
| `SchemaMismatch` from RapidAPI (if re-enabled) | Provider changed its response. Inspect one raw response and update `notam_rapidapi.rows`. |
| Days or hours look shifted | dbt forces `TimeZone: UTC` in `profiles.yml`. Ad hoc DuckDB sessions do not: run `set TimeZone='UTC'`. |
| Deploy fails on a seed column change | Should not happen: deploy runs `dbt seed --full-refresh`. Scheduled ingest runs do not, so let a deploy finish before the next ingest. |
| `Scheduled runs are not available on your plan` | The Flight was published with a cron, which the free plan does not allow. The deploy no longer sets one; GitHub Actions triggers the runs. |
| Flight run failed | `select * from md_get_flight_logs(flight_id := '<id>', run_number := <n>)`. Each source prints `FAILED - <reason>`; the run fails if any source or dbt failed. |
| Deploy: `Flight secret 'opensky' does not exist` | Add `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET` as GitHub secrets (or export them locally) and re-run. |
| Dive shows `Catalog does not exist` | The viewer cannot see `md:aviation`. Share the database with them. |
| dbt cannot find the database | dbt runs from `dbt/`. Use `make transform`, or export an absolute `WAREHOUSE` path. |

## 8. Next steps

1. Add credentials and run a week of OpenSky ingestion to see real coverage per airport.
2. Spot-check FAA NOTAM Search coverage for WSSS and EHAM against CAAS / LVNL, then try the Australian airports on it. Earlier options in [BACKLOG.md](BACKLOG.md).
3. Add the live NOTAM map (dropped with the old Vercel dashboard) to the Dive, so both the Dive and the Vercel site show it.
4. TAF skill: compare `stg_taf` forecasts with the METARs that followed.
5. Stretch: model excess terminal time from weather and NOTAM features.
