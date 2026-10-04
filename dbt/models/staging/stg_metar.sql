-- One row per METAR/SPECI. Units follow the Aviation Weather Center API:
-- wind in knots, visibility in statute miles ("6+" means 6 or more), altimeter in hPa.
with src as (
    select * from {{ source('raw', 'metar') }}
),

clouds as (
    select
        icao,
        obs_time,
        cast(payload -> 'clouds' as json[]) as cloud_layers,
        payload
    from src
)

select
    icao,
    to_timestamp(obs_time)                                        as observed_at,
    -- awc (Aviation Weather Center) or iem (archive, for history beyond AWC's 30 days).
    coalesce(payload ->> 'source', 'awc')                         as source,
    payload ->> 'metarType'                                       as report_type,
    payload ->> 'rawOb'                                           as raw_text,
    try_cast(payload ->> 'temp' as double)                        as temp_c,
    try_cast(payload ->> 'dewp' as double)                        as dewpoint_c,
    (payload ->> 'wdir') = 'VRB'                                  as wind_variable,
    try_cast(payload ->> 'wdir' as integer)                       as wind_dir_deg,
    try_cast(payload ->> 'wspd' as integer)                       as wind_speed_kt,
    try_cast(payload ->> 'wgst' as integer)                       as wind_gust_kt,
    try_cast(replace(payload ->> 'visib', '+', '') as double)     as visibility_sm,
    coalesce((payload ->> 'visib') like '%+', false)              as visibility_is_lower_bound,
    try_cast(payload ->> 'altim' as double)                       as altimeter_hpa,
    payload ->> 'wxString'                                        as wx_string,
    payload ->> 'fltCat'                                          as flight_category,
    -- Ceiling = lowest broken, overcast or obscured layer.
    list_min(list_transform(
        list_filter(cloud_layers, lambda c: (c ->> 'cover') in ('BKN', 'OVC', 'OVX')),
        lambda c: try_cast(c ->> 'base' as integer)
    ))                                                            as ceiling_ft
from clouds
