-- Taxi times at airports that publish gate times (Hong Kong): the airport's off-block and
-- on-block times paired with OpenSky's take-off and landing.
--   taxi-out  off-block ("Dep" on the board) to OpenSky first seen
--   taxi-in   OpenSky last seen to on-block ("At gate")
-- OpenSky's first / last seen are its first and last airborne positions; at Hong Kong they
-- behave as take-off and landing (Oct 2026: taxi-out median 19 min, taxi-in 7, none under
-- 2), and read a minute or two late / early where receivers miss the low end.
-- Flights pair by callsign (leading zeros dropped, as the board writes them) and time: the
-- first take-off within 90 min after off-block, the last landing within 60 min before
-- on-block. Each OpenSky flight pairs once, with the nearest gate time. OpenSky days only:
-- AeroDataBox's times are runway or revised, not comparable.
with gates as (
    select * from {{ ref('stg_hkia_flights') }}
    where gate_at is not null and callsign is not null
),

flights as (
    select
        flight_id,
        departure_icao,
        arrival_icao,
        first_seen_at,
        last_seen_at,
        regexp_replace(callsign, '^([A-Z]{3})0+([0-9])', '\1\2') as callsign_key
    from {{ ref('stg_flights') }}
    where source = 'opensky' and callsign is not null
),

paired as (
    select
        g.*,
        f.flight_id                                               as opensky_flight_id,
        case g.direction when 'departure' then f.first_seen_at else f.last_seen_at end as runway_at
    from gates g
    join flights f
      on f.callsign_key = g.callsign
     and ((g.direction = 'departure' and f.departure_icao = g.airport_icao
           and f.first_seen_at between g.gate_at and g.gate_at + interval 90 minute)
       or (g.direction = 'arrival' and f.arrival_icao = g.airport_icao
           and f.last_seen_at between g.gate_at - interval 60 minute and g.gate_at))
    -- The nearest OpenSky flight for each gate time, then the nearest gate time for each
    -- OpenSky flight.
    qualify row_number() over (partition by g.direction, g.flight, g.scheduled_at
                               order by abs(epoch(case g.direction when 'departure' then f.first_seen_at
                                                                  else f.last_seen_at end)
                                            - epoch(g.gate_at))) = 1
),

taxi as (
    select
        *,
        abs(date_diff('second', gate_at, runway_at)) / 60.0       as taxi_minutes
    from paired
    qualify row_number() over (partition by opensky_flight_id order by taxi_minutes) = 1
)

select
    airport_icao,
    direction,
    flight,
    callsign,
    is_cargo,
    scheduled_at,
    gate_at,
    runway_at,
    taxi_minutes,
    board_date,
    opensky_flight_id
from taxi
-- Under a minute is a coincidence of callsign and clock, not a taxi.
where taxi_minutes >= 1
