-- One row per observed flight. OpenSky estimates departure/arrival airports from
-- ADS-B positions, so either can be null when coverage near the airport is poor.
-- Airports and callsigns arrive in ICAO form (YSSY, QFA627); the IATA forms (SYD, QF627)
-- are looked up from the reference seeds and stored alongside.
with flights as (
    select
        *,
        nullif(trim(callsign), '') as callsign_icao
    from {{ source('raw', 'opensky_flights') }}
)

select
    f.icao24,
    f.callsign_icao                                         as callsign,
    -- Airline callsigns are usually the ICAO designator plus the flight number (QFA627),
    -- which maps to the IATA flight number (QF627). Alphanumeric ATC callsigns (QLK10D),
    -- registrations and military callsigns are not flight numbers and stay null.
    -- Leading zeros are dropped (SIA0231 -> SQ231).
    case when regexp_full_match(f.callsign_icao, '[A-Z]{3}[0-9]{1,4}')
              and al.iata is not null
         then al.iata || regexp_extract(f.callsign_icao, '^[A-Z]{3}0*([0-9]{1,4})$', 1)
    end                                                     as flight_number_iata,
    al.name                                                 as airline_name,
    f.est_departure_airport                                 as departure_icao,
    dep.iata                                                as departure_iata,
    f.est_arrival_airport                                   as arrival_icao,
    arr.iata                                                as arrival_iata,
    f.est_departure_airport || '-' || f.est_arrival_airport as route,
    to_timestamp(f.first_seen)                              as first_seen_at,
    to_timestamp(f.last_seen)                               as last_seen_at,
    f.first_seen                                            as first_seen_epoch,
    f.last_seen                                             as last_seen_epoch,
    (f.last_seen - f.first_seen) / 60.0                     as observed_minutes
from flights f
left join {{ ref('airlines') }} al on al.icao = left(f.callsign_icao, 3)
left join {{ ref('airport_codes') }} dep on dep.icao = f.est_departure_airport
left join {{ ref('airport_codes') }} arr on arr.icao = f.est_arrival_airport
