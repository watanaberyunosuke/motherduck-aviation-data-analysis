-- One row per waypoint. OpenSky path element: [time, lat, lon, baro_alt_m, true_track, on_ground]
with points as (
    select
        icao24,
        start_time,
        unnest(cast(payload -> 'path' as json[])) as p
    from {{ source('raw', 'opensky_tracks') }}
)

select
    icao24,
    start_time                                     as track_start_epoch,
    to_timestamp(cast(p ->> 0 as bigint))          as point_at,
    try_cast(p ->> 1 as double)                    as lat,
    try_cast(p ->> 2 as double)                    as lon,
    try_cast(p ->> 3 as double)                    as baro_alt_m,
    try_cast(p ->> 4 as double)                    as true_track_deg,
    coalesce(try_cast(p ->> 5 as boolean), false)  as on_ground
from points
