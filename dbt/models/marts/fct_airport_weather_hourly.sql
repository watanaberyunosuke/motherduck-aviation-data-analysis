-- Latest observation per airport per UTC hour, with the flags the impact analysis uses.
with ranked as (
    select
        *,
        date_trunc('hour', observed_at) as hour_utc
    from {{ ref('stg_metar') }}
    qualify row_number() over (partition by icao, date_trunc('hour', observed_at)
                               order by observed_at desc) = 1
)

select
    r.icao,
    a.iata,
    r.hour_utc,
    observed_at,
    report_type,
    flight_category,
    flight_category in ('IFR', 'LIFR')                    as is_ifr,
    wind_dir_deg,
    wind_variable,
    wind_speed_kt,
    wind_gust_kt,
    coalesce(wind_gust_kt, 0) >= 25                       as is_gusty,
    visibility_sm,
    visibility_is_lower_bound,
    ceiling_ft,
    wx_string,
    coalesce(wx_string like '%TS%', false)                as has_thunderstorm,
    coalesce(regexp_matches(wx_string, '(RA|SN|DZ|SH)'), false) as has_precipitation,
    temp_c,
    dewpoint_c,
    altimeter_hpa,
    raw_text
from ranked r
join {{ ref('airports') }} a on a.icao = r.icao
