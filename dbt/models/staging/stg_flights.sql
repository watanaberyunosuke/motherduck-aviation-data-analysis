-- One row per flight from the preferred source for its slot. AeroDataBox (schedules plus
-- actual times) wins for every (airport, direction, UTC day) it has loaded; OpenSky
-- (ADS-B only) fills the rest. An OpenSky flight is dropped when AeroDataBox covers
-- either of its in-scope ends, because AeroDataBox then has the same flight.
-- first_seen_* / last_seen_* keep OpenSky's names: for AeroDataBox they are the
-- departure and arrival times (runway, else revised, else scheduled).
with covered as (
    select icao, direction, day_utc
    from {{ source('raw', 'flight_slots') }}
    where source = 'aerodatabox'
),

aerodatabox as (
    select
        flight_id,
        'aerodatabox'                                       as source,
        icao24,
        callsign,
        flight_number_iata,
        airline_name,
        departure_icao,
        departure_iata,
        arrival_icao,
        arrival_iata,
        route,
        departed_at                                         as first_seen_at,
        arrived_at                                          as last_seen_at,
        cast(epoch(departed_at) as bigint)                  as first_seen_epoch,
        cast(epoch(arrived_at) as bigint)                   as last_seen_epoch,
        date_diff('second', departed_at, arrived_at) / 60.0 as observed_minutes,
        scheduled_departure_at,
        scheduled_arrival_at,
        departure_time_is_scheduled,
        arrival_time_is_scheduled,
        status
    from {{ ref('stg_aerodatabox_flights') }}
    where not is_cancelled
),

opensky as (
    select
        f.icao24 || '@' || f.first_seen_epoch               as flight_id,
        'opensky'                                           as source,
        f.icao24,
        f.callsign,
        f.flight_number_iata,
        f.airline_name,
        f.departure_icao,
        f.departure_iata,
        f.arrival_icao,
        f.arrival_iata,
        f.route,
        f.first_seen_at,
        f.last_seen_at,
        f.first_seen_epoch,
        f.last_seen_epoch,
        f.observed_minutes,
        null::timestamptz                                   as scheduled_departure_at,
        null::timestamptz                                   as scheduled_arrival_at,
        false                                               as departure_time_is_scheduled,
        false                                               as arrival_time_is_scheduled,
        null::varchar                                       as status
    from {{ ref('stg_opensky_flights') }} f
    where not exists (
        select 1 from covered c
        where c.icao = f.arrival_icao and c.direction = 'arrival'
          and c.day_utc = cast(f.last_seen_at as date))
      and not exists (
        select 1 from covered c
        where c.icao = f.departure_icao and c.direction = 'departure'
          and c.day_utc = cast(f.first_seen_at as date))
)

select * from aerodatabox
union all
select * from opensky
