-- Every arrival at an in-scope airport, from any origin, with the weather when it landed.
-- From AeroDataBox where loaded (with the schedule, so delay is measured), else OpenSky.
-- Delay is null where there is no schedule (OpenSky) or no actual time yet. Terminal-area metrics are attached only where a well-covered
-- track exists (fct_arrival_weather_impact); most arrivals have none, because the
-- /tracks quota covers only a fraction of the day's flights.
{% set metar_max_age = var('metar_max_age_minutes') %}

with arrivals as (
    select f.*
    from {{ ref('stg_flights') }} f
    join {{ ref('airports') }} a on a.icao = f.arrival_icao
    where f.last_seen_at is not null
),

with_weather as (
    select
        arr.*,
        w.observed_at as metar_observed_at,
        w.flight_category
    from arrivals arr
    asof left join {{ ref('stg_metar') }} w
      on w.icao = arr.arrival_icao
     and w.observed_at <= arr.last_seen_at
),

with_impact as (
    select
        ww.*,
        i.terminal_minutes,
        i.excess_terminal_minutes
    from with_weather ww
    left join {{ ref('fct_arrival_weather_impact') }} i
      on i.icao24 = ww.icao24
     and i.track_start_epoch between ww.first_seen_epoch - 1800
                                 and coalesce(ww.last_seen_epoch, ww.first_seen_epoch)
    qualify row_number() over (partition by ww.flight_id
                               order by i.track_start_epoch desc nulls last) = 1
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
    first_seen_at,
    -- OpenSky: last ADS-B position, at or shortly before touchdown. AeroDataBox: runway
    -- time, else revised (actual), else scheduled (arrival_time_is_scheduled).
    last_seen_at                                                  as arrived_at,
    scheduled_arrival_at                                          as scheduled_at,
    arrival_time_is_scheduled,
    case when not arrival_time_is_scheduled
         then date_diff('second', scheduled_arrival_at, last_seen_at) / 60.0 end
                                                                  as delay_minutes,
    status,
    observed_minutes,
    case when date_diff('minute', metar_observed_at, last_seen_at) <= {{ metar_max_age }}
         then flight_category end                                 as flight_category,
    terminal_minutes,
    excess_terminal_minutes
from with_impact
