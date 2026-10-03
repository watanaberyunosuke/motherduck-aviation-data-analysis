# Aviation data analysis

How much does weather and runway availability cost arriving flights at Sydney, Melbourne, Brisbane, Singapore and Hong Kong?

- Ingests METAR/TAF weather, NOTAMs (Hong Kong only for now) and ADS-B flight paths on a schedule into DuckDB (local) or MotherDuck (scheduled runs).
- Transforms with dbt into marts that line up each arrival with the weather and NOTAMs in force when it landed.
- Measures **excess terminal-area time**, the minutes an arrival spends within 50 NM of its destination beyond that airport's rolling median. This is the weather-sensitive part of a flight: holding, vectoring and go-arounds.
- Scheduled by a [MotherDuck Flight](https://motherduck.com/docs/concepts/flights/) and visualised in a Vercel dashboard plus a [MotherDuck Dive](https://motherduck.com/docs/key-tasks/dives/).
- Modelled on [colin-k-rogers/formula-1-data-analysis](https://github.com/colin-k-rogers/formula-1-data-analysis): scheduled ingest, then dbt, then dashboard.

## 1. Data sources

| Data | Source | Airports | Access | Cadence |
|---|---|---|---|---|
| METAR, TAF | [Aviation Weather Center Data API](https://aviationweather.gov/data/api/) | All five | Free, no key | Hourly |
| NOTAMs (Hong Kong) | [HK CAD NOTAM website](https://www.notam.ais.gov.hk/), JSON at `/data` | VHHH, VHHK FIR | Free, official, no key | Every 3 h |
| NOTAMs (AU, SG) | None at present | YSSY, YMML, YBBN, WSSS | See below | Not ingested |
| Flights, flight paths | [OpenSky Network REST API](https://openskynetwork.github.io/opensky-api/rest.html) | All five | Free account, OAuth2 client | Daily |

Australia (Airservices NAIPS) and Singapore (CAAS AIM-SG) publish live NOTAMs only to registered users, and there is no free third-party feed. A client for the paid [SkyLink NOTAM API](https://github.com/SkyLink-API/notam-api) on RapidAPI (Basic plan $18.59/month for 5,000 requests; no free tier) is kept in `src/aviation/sources/notam_rapidapi.py` but is switched off. To re-enable it, set `notam_source: rapidapi` for those airports in `config/airports.yml` and `dbt/seeds/airports.csv`, add `RAPIDAPI_KEY`, and restore the 6-hourly schedule in `ingest.yml`.

## 2. Architecture

```
                         src/aviation            dbt/models                     ┌─> Vercel dashboard (api/)
aviationweather.gov ─┐
notam.ais.gov.hk ────┼─> ingest (Python) ──> raw.* ──> staging.* ──> marts.* ──┤
OpenSky Network ─────┘   idempotent upserts,        views          tables      └─> MotherDuck Dive (dives/)
                         full JSON payload kept
        └──────────── both run hourly in the MotherDuck Flight aviation_pipeline (flights/) ────────────┘
```

- **Raw layer** (`src/aviation/warehouse.py`): one table per source, keyed on the natural key, with the full payload as JSON. Re-running any fetch overwrites rather than duplicates.
- **NOTAM parsing** (`src/aviation/parsing/notam.py`): ICAO items A to G and the Q-line are parsed at load time. Items are read in their fixed order so text such as `TWY C)` is not mistaken for item C. NOTAMs record `first_seen_at` and `last_seen_at`, so a NOTAM dropping out of a feed (cancelled or expired) is visible.
- **Marts** (`dbt/models/marts`):

| Model | Grain | Purpose |
|---|---|---|
| `fct_airport_weather_hourly` | airport, UTC hour | Latest METAR with IFR, gust and thunderstorm flags |
| `fct_notams` | NOTAM per source | Category from Q-code, runway-closure and in-force flags |
| `fct_daily_airport_movements` | airport, UTC day | Observed arrivals and departures |
| `fct_flight_track_metrics` | tracked flight | Path length, route inefficiency, terminal-area time, coverage check |
| `fct_arrival_weather_impact` | arrival | **Primary table.** Excess terminal time plus weather and NOTAMs at arrival. NOTAM flags are null where `has_notam_feed` is false |

## 3. Why terminal-area time, not delay

OpenSky observes aircraft; it has no timetables. Without scheduled times, schedule delay cannot be calculated. Terminal-area time is observable from the flight path, responds directly to weather and runway capacity, and is comparable across airports once each airport is benchmarked against its own median.

To add true schedule delay later you would need a schedules source (these are generally paid) and a join on callsign and date.

## 4. Setup

```bash
conda create -n aviation python=3.11 -y && conda activate aviation
pip install -e ".[dev]"
cp .env.example .env        # fill in OPENSKY_*

make ingest-weather          # works with no credentials
aviation ingest notam-hk     # works with no credentials
make ingest-all              # needs OpenSky credentials
make transform               # dbt seed + run + test
make test                    # pytest, including a full dbt build on a temp database
```

Credentials:

- **OpenSky**: create a free account, then Account > API client. New accounts must use OAuth2 client credentials.
- **MotherDuck** (scheduled runs only): set `WAREHOUSE=md:aviation` and `MOTHERDUCK_TOKEN`. Check MotherDuck's current free-tier limits before relying on it.

## 5. Scheduling and dashboards

### Flight: `aviation_pipeline`

A [MotherDuck Flight](https://motherduck.com/docs/concepts/flights/) runs the pipeline at :07 every hour (UTC). Each run downloads the commit it is pinned to from GitHub, runs the ingest sources due that hour, then `dbt build`. Ingest and dbt run in one process, so they never write to the warehouse at the same time.

| UTC hour | Sources |
|---|---|
| Every hour | METAR, TAF |
| 0, 3, 6, … 21 | + Hong Kong NOTAMs |
| 6 | + OpenSky flights and tracks for yesterday |

The source is `flights/aviation_pipeline/main.py`. OpenSky credentials come from a Flight secret named `opensky`, which the deploy creates from the GitHub secrets. To run it now, or with other sources:

```sql
-- flight_id from: select flight_id from md_list_flights() where flight_name = 'aviation_pipeline'
select * from md_run_flight(flight_id := '<id>', config := MAP {'SOURCES': 'opensky'});
select * from md_list_flight_runs(flight_id := '<id>') order by run_number desc limit 5;
select * from md_get_flight_logs(flight_id := '<id>', run_number := <n>);
```

`SOURCES` takes space-separated sources (`metar taf notam-hk opensky`), or `none` for dbt only. Cron scheduling needs a MotherDuck plan that includes scheduled Flights (Lite unlimited, Business or Enterprise).

`.github/workflows/ingest.yml` is now a manual fallback (`workflow_dispatch` only) that runs one source and dbt from GitHub Actions.

### Dive: Airport conditions

`dives/airport_conditions/index.tsx` is a [MotherDuck Dive](https://motherduck.com/docs/key-tasks/dives/), a React component that MotherDuck hosts and that queries `aviation.marts` live. The Vercel dashboard shows fleet-wide averages and the NOTAM map. The Dive drills into one airport: 7-day weather shares for every airport, 72 hours of wind and flight category, daily movements, median excess terminal time with and without each weather condition, and the slowest arrivals. The selected airport is kept in the URL, so a link opens the same view.

To preview edits with hot reload, install the [MotherDuck CLI](https://motherduck.com/docs/sql-reference/motherduck-cli/) and run `motherduck dive watch dives/airport_conditions`. Only `index.tsx` and `dive.metadata.json` (title, description) are deployed.

The Dive queries `md:aviation` directly, so viewers need access to that database. To share the Dive with someone else in your organisation, share the database with them first.

### Deploying

`scripts/deploy_motherduck.py` (or `make deploy-motherduck`) publishes both. It matches the Flight by name and the Dive by title, creates them if missing and updates them otherwise; every update is a new version in MotherDuck. Run locally, it refuses a commit that is not yet on GitHub, because the Flight would fail to download it.

`ci.yml` is the CI/CD pipeline:

- **test**: on every push and pull request, runs `pytest` (including a full dbt build on a temporary DuckDB file) on Python 3.11 and 3.14.
- **deploy**: on pushes to `main`, after tests pass, rebuilds seeds and runs `dbt build` against MotherDuck (`md:aviation`), creating the database if it does not exist. It then points the Flight at the new commit and publishes the Dive. It uses the `production` environment, so you can add required reviewers under Settings > Environments. It needs the `MOTHERDUCK_TOKEN`, `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET` secrets.

Deploy and the manual ingest workflow share a concurrency group, so they never write to the warehouse at the same time. The Flight is outside that group: a deploy that lands at :07 can overlap the hourly run. If dbt then fails on a conflicting write, the next hourly run rebuilds. Both install dependencies from `uv.lock`; after changing dependencies in `pyproject.toml`, run `uv lock` and commit the lockfile.

## 6. Known limitations

- **No NOTAMs for Australia or Singapore.** Only Hong Kong has a NOTAM feed, so runway closures cannot explain excess terminal time at YSSY, YMML, YBBN or WSSS. For those arrivals `surface_notam_in_force` and `runway_closure_in_force` are null (unknown), not false. Filter on `has_notam_feed` before comparing NOTAM effects across airports.
- **SkyLink client is untested against live data.** If it is re-enabled, check on the first real call whether YSSY and WSSS NOTAMs arrive in ICAO format (the provider's example is FAA domestic format, with no Q-line) and spot-check completeness against NAIPS / AIM-SG. A response shape change raises `SchemaMismatch` rather than loading bad rows.
- **OpenSky coverage in Australia and Asia is thinner than in Europe and North America.** `fct_flight_track_metrics.has_full_coverage` is only true when the track is seen within 30 km of both airports; the impact mart uses only those flights. Expect a lower share of usable flights than in Europe.
- **OpenSky quotas.** Tracks are limited to the last 30 days and the endpoint is marked experimental. The client reads `X-Rate-Limit-Remaining`, stops below `min_credits_remaining`, and caps calls at `max_tracks_per_run` (`config/airports.yml`).
- **NOTAM schedules.** A NOTAM with a D) schedule is only active in its sub-windows. `has_schedule` flags these; the in-force checks currently treat the whole B) to C) window as active, which overstates them.
- **HK feed terms.** The CAD site describes its data as informational. Read its Important Notices before using the data beyond analysis.
- **MotherDuck path untested here.** Local DuckDB was tested end to end; `md:` targets rely on DuckDB's MotherDuck extension and have not been run in this build.

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Environment variable X is not set` | Copy `.env.example` to `.env`, or add the GitHub secret. Other sources still run. |
| OpenSky `403 You cannot access historical flights` | Missing or invalid OAuth client. Check `OPENSKY_CLIENT_ID` / `SECRET`. |
| OpenSky run logs `stopped early` | Daily credit floor reached. Lower `max_tracks_per_run` or run later; flights already landed are kept. |
| `SchemaMismatch` from RapidAPI (if re-enabled) | Provider changed its response. Inspect one raw response and update `notam_rapidapi.rows`. |
| Days or hours look shifted | dbt forces `TimeZone: UTC` in `profiles.yml`. Ad hoc DuckDB sessions do not: run `set TimeZone='UTC'`. |
| Deploy fails on a seed column change | Should not happen: deploy runs `dbt seed --full-refresh`. Scheduled ingest runs do not, so let a deploy finish before the next ingest. |
| Flight run failed | `select * from md_get_flight_logs(flight_id := '<id>', run_number := <n>)`. Each source prints `FAILED - <reason>`; the run fails if any source or dbt failed. |
| Deploy: `Flight secret 'opensky' does not exist` | Add `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET` as GitHub secrets (or export them locally) and re-run. |
| Dive shows `Catalog does not exist` | The viewer cannot see `md:aviation`. Share the database with them. |
| dbt cannot find the database | dbt runs from `dbt/`. Use `make transform`, or export an absolute `WAREHOUSE` path. |

## 8. Next steps

1. Add credentials and run a week of OpenSky ingestion to see real coverage per airport.
2. Find a NOTAM source for Australia and Singapore. Parked; options and plan in [BACKLOG.md](BACKLOG.md).
3. Dashboard on `fct_arrival_weather_impact`: excess terminal minutes by airport and weather condition, plus a live map of current NOTAMs.
4. TAF skill: compare `stg_taf` forecasts with the METARs that followed.
5. Stretch: model excess terminal time from weather and NOTAM features.
