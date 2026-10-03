-- The analysis table: each well-covered arrival at an in-scope airport, the weather and
-- NOTAM conditions at the time, and how its terminal-area time compares with the
-- destination's recent baseline.
{% set baseline_days = var('baseline_days') %}
{% set metar_max_age = var('metar_max_age_minutes') %}

with arrivals as (
    select m.*, a.notam_source is not null as has_notam_feed
    from {{ ref('fct_flight_track_metrics') }} m
    join {{ ref('airports') }} a on a.icao = m.arrival_icao
    where m.has_full_coverage
      and m.terminal_entry_at is not null
),

with_weather as (
    select
        arr.*,
        w.observed_at        as metar_observed_at,
        w.flight_category,
        w.wind_speed_kt,
        w.wind_gust_kt,
        w.visibility_sm,
        w.ceiling_ft,
        w.wx_string
    from arrivals arr
    asof left join {{ ref('stg_metar') }} w
      on w.icao = arr.arrival_icao
     and w.observed_at <= arr.last_airborne_at
),

with_notams as (
    select
        ww.*,
        -- Null, not false, when the airport has no NOTAM feed: absence of data is not
        -- absence of a closure.
        case when ww.has_notam_feed then exists (
            select 1 from {{ ref('fct_notams') }} n
            where n.location = ww.arrival_icao
              and n.is_surface_or_approach
              and n.starts_at <= ww.last_airborne_at
              and (n.ends_at is null or n.ends_at > ww.last_airborne_at)
        ) end as surface_notam_in_force,
        case when ww.has_notam_feed then exists (
            select 1 from {{ ref('fct_notams') }} n
            where n.location = ww.arrival_icao
              and n.is_runway_closure
              and n.starts_at <= ww.last_airborne_at
              and (n.ends_at is null or n.ends_at > ww.last_airborne_at)
        ) end as runway_closure_in_force
    from with_weather ww
)

select
    icao24,
    track_start_epoch,
    callsign,
    route,
    departure_icao,
    arrival_icao,
    last_airborne_at                                   as arrived_at,
    terminal_minutes,
    route_inefficiency,
    -- Baseline excludes the flight itself so an outlier cannot pull its own benchmark.
    quantile_cont(terminal_minutes, 0.5) over baseline as baseline_terminal_minutes,
    count(*) over baseline                             as baseline_flights,
    terminal_minutes - quantile_cont(terminal_minutes, 0.5) over baseline
                                                       as excess_terminal_minutes,
    -- Weather is only attached when the METAR is recent enough to describe the arrival.
    date_diff('minute', metar_observed_at, last_airborne_at) <= {{ metar_max_age }}
                                                       as has_current_metar,
    flight_category,
    flight_category in ('IFR', 'LIFR')                 as is_ifr,
    wind_speed_kt,
    wind_gust_kt,
    visibility_sm,
    ceiling_ft,
    wx_string,
    coalesce(wx_string like '%TS%', false)             as has_thunderstorm,
    has_notam_feed,
    surface_notam_in_force,
    runway_closure_in_force
from with_notams
window baseline as (
    partition by arrival_icao
    order by last_airborne_at
    range between interval {{ baseline_days }} days preceding and current row
    exclude current row
)
