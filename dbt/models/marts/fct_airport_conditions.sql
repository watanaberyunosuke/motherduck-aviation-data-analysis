-- One row per in-scope airport: where it is, its time zone, and the latest METAR, TAF
-- and NOTAM count. Feeds the dashboards' "current conditions" panel and map.
with latest_metar as (
    select *
    from {{ ref('stg_metar') }}
    qualify row_number() over (partition by icao order by observed_at desc) = 1
),

-- stg_taf has one row per forecast period; any row of the newest TAF carries its text.
latest_taf as (
    select icao, issued_at, valid_from, valid_to, raw_text
    from {{ ref('stg_taf') }}
    qualify row_number() over (partition by icao order by issued_at desc, period_from) = 1
),

notams as (
    select location, count(*) as n
    from {{ ref('fct_notams') }}
    where is_current
    group by 1
)

select
    a.icao,
    a.iata,
    a.name,
    a.lat,
    a.lon,
    a.timezone,
    m.observed_at                 as metar_observed_at,
    m.raw_text                    as metar_raw,
    m.flight_category,
    m.wind_variable,
    m.wind_dir_deg,
    m.wind_speed_kt,
    m.wind_gust_kt,
    m.visibility_sm,
    m.visibility_is_lower_bound,
    m.ceiling_ft,
    m.wx_string,
    m.temp_c,
    m.dewpoint_c,
    m.altimeter_hpa,
    t.issued_at                   as taf_issued_at,
    t.valid_from                  as taf_valid_from,
    t.valid_to                    as taf_valid_to,
    t.raw_text                    as taf_raw,
    -- Null, not 0, where the airport has no NOTAM feed.
    case when a.notam_source is not null then coalesce(n.n, 0) end as notams_in_force
from {{ ref('airports') }} a
left join latest_metar m on m.icao = a.icao
left join latest_taf t on t.icao = a.icao
left join notams n on n.location = a.icao
