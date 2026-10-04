-- One row per flight from the preferred source for its slot. OpenSky (ADS-B) wins for
-- every (airport, direction, UTC day) it has loaded flights for; AeroDataBox (schedules
-- plus actual times) fills the rest. An AeroDataBox flight is dropped when OpenSky covers
-- either of its in-scope ends, because OpenSky then has the same flight. A slot OpenSky
-- loaded empty does not count as covered: that is a gap in its receivers, not a quiet day.
-- first_seen_* / last_seen_* keep OpenSky's names: for AeroDataBox they are the
-- departure and arrival times (runway, else revised, else scheduled).
with covered as (
    select icao, direction, day_utc
    from {{ source('raw', 'flight_slots') }}
    where source = 'opensky' and rows > 0
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
    from {{ ref('stg_aerodatabox_flights') }} f
    where not is_cancelled
      and not exists (
        select 1 from covered c
        where c.icao = f.arrival_icao and c.direction = 'arrival'
          and c.day_utc = cast(f.arrived_at as date))
      and not exists (
        select 1 from covered c
        where c.icao = f.departure_icao and c.direction = 'departure'
          and c.day_utc = cast(f.departed_at as date))
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
)

select * from opensky
union all
select * from aerodatabox
