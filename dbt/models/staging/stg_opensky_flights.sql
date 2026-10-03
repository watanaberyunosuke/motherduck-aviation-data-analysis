-- One row per observed flight. OpenSky estimates departure/arrival airports from
-- ADS-B positions, so either can be null when coverage near the airport is poor.
select
    icao24,
    callsign,
    est_departure_airport                                   as departure_icao,
    est_arrival_airport                                     as arrival_icao,
    est_departure_airport || '-' || est_arrival_airport     as route,
    to_timestamp(first_seen)                                as first_seen_at,
    to_timestamp(last_seen)                                 as last_seen_at,
    first_seen                                              as first_seen_epoch,
    last_seen                                               as last_seen_epoch,
    (last_seen - first_seen) / 60.0                         as observed_minutes
from {{ source('raw', 'opensky_flights') }}
