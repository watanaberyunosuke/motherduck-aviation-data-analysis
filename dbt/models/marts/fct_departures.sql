-- Every departure from an in-scope airport, to any destination (AeroDataBox where loaded,
-- with its schedule, else OpenSky), with the weather when it took off and, where a well-covered track exists, how long it took to
-- leave the departure terminal area against the airport's rolling median.
-- Tracks are fetched for arrivals at in-scope airports (config/airports.yml), so only
-- departures bound for another in-scope airport can have terminal metrics.
{% set baseline_days = var('baseline_days') %}
{% set metar_max_age = var('metar_max_age_minutes') %}

with departures as (
    select f.*
    from {{ ref('stg_flights') }} f
    join {{ ref('airports') }} a on a.icao = f.departure_icao
    where f.first_seen_at is not null
),

with_weather as (
    select
        d.*,
        w.observed_at as metar_observed_at,
        w.flight_category
    from departures d
    asof left join {{ ref('stg_metar') }} w
      on w.icao = d.departure_icao
     and w.observed_at <= d.first_seen_at
),

with_track as (
    select
        ww.*,
        case when m.has_departure_coverage then m.departure_terminal_minutes end
            as departure_terminal_minutes
    from with_weather ww
    left join {{ ref('fct_flight_track_metrics') }} m
      on m.icao24 = ww.icao24
     and m.track_start_epoch between ww.first_seen_epoch - 1800
                                 and coalesce(ww.last_seen_epoch, ww.first_seen_epoch)
    qualify row_number() over (partition by ww.flight_id
                               order by m.has_departure_coverage desc nulls last,
                                        m.track_start_epoch desc nulls last) = 1
)

select
    flight_id,
    source,
    icao24,
    first_seen_epoch,
    callsign,
    flight_number_iata,
    airline_name,
    departure_icao,
    departure_iata,
    arrival_icao,
    arrival_iata,
    -- OpenSky: first ADS-B position, at or shortly after take-off. AeroDataBox: runway
    -- time, else revised (actual), else scheduled (departure_time_is_scheduled).
    first_seen_at                                                   as departed_at,
    scheduled_departure_at                                          as scheduled_at,
    departure_time_is_scheduled,
    case when not departure_time_is_scheduled
         then date_diff('second', scheduled_departure_at, first_seen_at) / 60.0 end
                                                                    as delay_minutes,
    status,
    last_seen_at,
    case when date_diff('minute', metar_observed_at, first_seen_at) <= {{ metar_max_age }}
         then flight_category end                                   as flight_category,
    departure_terminal_minutes,
    -- Baseline excludes the flight itself, as for arrivals.
    quantile_cont(departure_terminal_minutes, 0.5) over baseline    as baseline_departure_minutes,
    departure_terminal_minutes
      - quantile_cont(departure_terminal_minutes, 0.5) over baseline as excess_departure_minutes
from with_track
window baseline as (
    partition by departure_icao
    order by first_seen_at
    range between interval {{ baseline_days }} days preceding and current row
    exclude current row
)
