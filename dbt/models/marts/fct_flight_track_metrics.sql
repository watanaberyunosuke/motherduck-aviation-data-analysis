-- One row per tracked flight: how far it actually flew versus the great-circle distance,
-- and how long it spent inside the destination terminal area before landing.
-- Terminal-area time is the weather-sensitive part of a flight (holding, vectoring,
-- go-arounds), so it stands in for delay, which OpenSky cannot measure (no schedules).
-- Airport coordinates come from the worldwide airport_codes seed, so flights from
-- origins outside the in-scope list are measured too.
{% set terminal_km = var('terminal_radius_km') %}

with points as (
    select * from {{ ref('stg_opensky_track_points') }}
),

flights as (
    select * from {{ ref('stg_flights') }}
    where icao24 is not null
),

-- Attach each track to the flight it belongs to.
tracks as (
    select
        t.icao24,
        t.track_start_epoch,
        f.callsign,
        f.flight_number_iata,
        f.departure_icao,
        f.departure_iata,
        f.arrival_icao,
        f.arrival_iata,
        f.route
    from (select distinct icao24, track_start_epoch from points) t
    join flights f
      on f.icao24 = t.icao24
     and t.track_start_epoch between f.first_seen_epoch - 1800
                                 and coalesce(f.last_seen_epoch, f.first_seen_epoch)
    qualify row_number() over (partition by t.icao24, t.track_start_epoch
                               order by abs(t.track_start_epoch - f.first_seen_epoch)) = 1
),

airborne as (
    select
        p.icao24,
        p.track_start_epoch,
        p.point_at,
        p.lat,
        p.lon,
        dep.lat as dep_lat, dep.lon as dep_lon,
        arr.lat as arr_lat, arr.lon as arr_lon,
        lag(p.lat) over w as prev_lat,
        lag(p.lon) over w as prev_lon,
        lag(p.point_at) over w as prev_at
    from points p
    join tracks tr using (icao24, track_start_epoch)
    left join {{ ref('airport_codes') }} dep on dep.icao = tr.departure_icao
    left join {{ ref('airport_codes') }} arr on arr.icao = tr.arrival_icao
    where not p.on_ground and p.lat is not null and p.lon is not null
    window w as (partition by p.icao24, p.track_start_epoch order by p.point_at)
),

segments as (
    select
        *,
        case when prev_lat is null then 0
             else {{ haversine_km('prev_lat', 'prev_lon', 'lat', 'lon') }} end as segment_km,
        date_diff('second', prev_at, point_at) / 60.0                           as gap_minutes,
        {{ haversine_km('lat', 'lon', 'arr_lat', 'arr_lon') }}                  as km_to_arrival,
        {{ haversine_km('lat', 'lon', 'dep_lat', 'dep_lon') }}                  as km_from_departure
    from airborne
),

per_track as (
    select
        icao24,
        track_start_epoch,
        count(*)                                                   as airborne_points,
        min(point_at)                                              as first_airborne_at,
        max(point_at)                                              as last_airborne_at,
        sum(segment_km)                                            as path_km,
        max(gap_minutes)                                           as max_gap_minutes,
        arg_min(km_from_departure, point_at)                       as first_point_km_from_departure,
        arg_min(km_to_arrival, point_at)                           as first_point_km_from_arrival,
        arg_max(km_to_arrival, point_at)                           as last_point_km_from_arrival,
        min(point_at) filter (where km_to_arrival <= {{ terminal_km }}) as terminal_entry_at,
        min(point_at) filter (where km_from_departure > {{ terminal_km }}) as departure_exit_at
    from segments
    group by 1, 2
)

select
    tr.icao24,
    tr.track_start_epoch,
    tr.callsign,
    tr.flight_number_iata,
    tr.departure_icao,
    tr.departure_iata,
    tr.arrival_icao,
    tr.arrival_iata,
    tr.route,
    pt.first_airborne_at,
    pt.last_airborne_at,
    date_diff('second', pt.first_airborne_at, pt.last_airborne_at) / 60.0  as airborne_minutes,
    pt.airborne_points,
    pt.max_gap_minutes,
    pt.path_km,
    {{ haversine_km('dep.lat', 'dep.lon', 'arr.lat', 'arr.lon') }}           as great_circle_km,
    pt.path_km / nullif({{ haversine_km('dep.lat', 'dep.lon', 'arr.lat', 'arr.lon') }}, 0)
                                                                           as route_inefficiency,
    pt.terminal_entry_at,
    date_diff('second', pt.terminal_entry_at, pt.last_airborne_at) / 60.0  as terminal_minutes,
    pt.first_point_km_from_departure,
    pt.last_point_km_from_arrival,
    -- Path length and route inefficiency need the track seen near both runways.
    coalesce(pt.first_point_km_from_departure <= 30
             and pt.last_point_km_from_arrival <= 30, false)               as has_full_coverage,
    -- Terminal time only needs the arrival end: seen outside the terminal area before
    -- entering it, and close to the runway at the end. The departure can be anywhere.
    coalesce(pt.first_point_km_from_arrival > {{ terminal_km }}
             and pt.last_point_km_from_arrival <= 30, false)               as has_arrival_coverage,
    -- The departure mirror: from first airborne point to the first point outside the
    -- departure airport's terminal area (climb-out, departure procedure, vectors).
    pt.departure_exit_at,
    date_diff('second', pt.first_airborne_at, pt.departure_exit_at) / 60.0 as departure_terminal_minutes,
    coalesce(pt.first_point_km_from_departure <= 30
             and pt.departure_exit_at is not null, false)                  as has_departure_coverage
from tracks tr
join per_track pt using (icao24, track_start_epoch)
left join {{ ref('airport_codes') }} dep on dep.icao = tr.departure_icao
left join {{ ref('airport_codes') }} arr on arr.icao = tr.arrival_icao
