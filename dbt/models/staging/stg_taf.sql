-- One row per TAF forecast period (base period plus each BECMG/TEMPO/FM/PROB group).
with src as (
    select * from {{ source('raw', 'taf') }}
),

periods as (
    select
        icao,
        issue_time,
        payload,
        unnest(cast(payload -> 'fcsts' as json[])) as period
    from src
)

select
    icao,
    cast(issue_time as timestamptz)                                  as issued_at,
    to_timestamp(cast(payload ->> 'validTimeFrom' as bigint))        as valid_from,
    to_timestamp(cast(payload ->> 'validTimeTo' as bigint))          as valid_to,
    payload ->> 'rawTAF'                                             as raw_text,
    to_timestamp(cast(period ->> 'timeFrom' as bigint))              as period_from,
    to_timestamp(cast(period ->> 'timeTo' as bigint))                as period_to,
    coalesce(period ->> 'fcstChange', 'BASE')                        as change_type,
    try_cast(period ->> 'probability' as integer)                    as probability_pct,
    try_cast(period ->> 'wdir' as integer)                           as wind_dir_deg,
    try_cast(period ->> 'wspd' as integer)                           as wind_speed_kt,
    try_cast(period ->> 'wgst' as integer)                           as wind_gust_kt,
    try_cast(replace(period ->> 'visib', '+', '') as double)         as visibility_sm,
    period ->> 'wxString'                                            as wx_string,
    list_min(list_transform(
        list_filter(cast(period -> 'clouds' as json[]),
                    lambda c: (c ->> 'cover') in ('BKN', 'OVC', 'OVX')),
        lambda c: try_cast(c ->> 'base' as integer)
    ))                                                               as ceiling_ft
from periods
