-- Flight-path points near each in-scope airport, for the dashboards' map: the arrival end
-- of tracks landing there and the departure end of tracks leaving it. Thinned to one point
-- per 30 s per track, which keeps the shape of the approach and departure procedures
-- actually flown while keeping the export small.
{% set radius_km = var('map_radius_km') %}

with tracks as (
    select icao24, track_start_epoch, callsign, flight_number_iata,
           departure_icao, departure_iata, arrival_icao, arrival_iata
    from {{ ref('fct_flight_track_metrics') }}
),

roles as (
    select t.*, 'arrival' as role, t.arrival_icao as airport_icao
    from tracks t
    join {{ ref('airports') }} a on a.icao = t.arrival_icao
    union all
    select t.*, 'departure', t.departure_icao
    from tracks t
    join {{ ref('airports') }} a on a.icao = t.departure_icao
)

select
    r.airport_icao,
    r.role,
    r.icao24,
    r.track_start_epoch,
    r.callsign,
    r.flight_number_iata,
    r.departure_iata,
    r.arrival_iata,
    p.point_at,
    p.lat,
    p.lon,
    p.baro_alt_m
from roles r
join {{ ref('stg_opensky_track_points') }} p using (icao24, track_start_epoch)
join {{ ref('airports') }} a on a.icao = r.airport_icao
where not p.on_ground
  and p.lat is not null
  and {{ haversine_km('p.lat', 'p.lon', 'a.lat', 'a.lon') }} <= {{ radius_km }}
qualify row_number() over (
    partition by r.role, r.airport_icao, r.icao24, r.track_start_epoch,
                 time_bucket(interval 30 second, p.point_at)
    order by p.point_at) = 1
