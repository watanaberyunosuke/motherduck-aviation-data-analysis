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

-- Newest reading per airport and source from outside the METAR (raw.wx_extra), no older
-- than three hours. A field comes from the best source that has it: the national weather
-- service, then Open-Meteo, then MET Norway.
extra_ranked as (
    select
        icao, source, temp_c, dewpoint_c, wx_text,
        case source when 'gov' then 1 when 'open-meteo' then 2 else 3 end as priority
    from {{ source('raw', 'wx_extra') }}
    where observed_at >= now() - interval 3 hour
    qualify row_number() over (partition by icao, source order by observed_at desc) = 1
),

extra as (
    select
        icao,
        arg_min(temp_c, priority) filter (where temp_c is not null)       as temp_c,
        arg_min(source, priority) filter (where temp_c is not null)       as temp_source,
        arg_min(dewpoint_c, priority) filter (where dewpoint_c is not null) as dewpoint_c,
        arg_min(source, priority) filter (where dewpoint_c is not null)   as dewpoint_source,
        arg_min(wx_text, priority) filter (where wx_text is not null)     as wx_text,
        arg_min(source, priority) filter (where wx_text is not null)      as wx_text_source
    from extra_ranked
    group by icao
),

-- The ATIS text last seen within three hours (Hong Kong only).
latest_atis as (
    select icao, arrival_letter, departure_letter, text, last_seen_at
    from {{ source('raw', 'atis') }}
    where last_seen_at >= now() - interval 3 hour
    qualify row_number() over (partition by icao order by last_seen_at desc) = 1
),

notams as (
    select location, count(*) as n
    from {{ ref('fct_notams') }}
    where is_current
    group by 1
),

-- Sources that have loaded at least once. An airport whose source never has (an FAA key
-- not yet set, say) has no feed in practice.
notam_feeds as (
    select distinct source from {{ ref('stg_notam') }}
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
    -- Weather text from outside the METAR only while the METAR is missing or stale. A fresh
    -- METAR with no present-weather group means nothing is happening ("Nil"), and a model
    -- value must not contradict it.
    case when not coalesce(mf.is_fresh, false) then e.wx_text end        as wx_text,
    case when not coalesce(mf.is_fresh, false) then e.wx_text_source end as wx_text_source,
    -- A fresh METAR's own value wins; otherwise the best outside source, then the stale METAR.
    case when coalesce(mf.is_fresh, false) and m.temp_c is not null then m.temp_c
         else coalesce(e.temp_c, m.temp_c) end                             as temp_c,
    case when coalesce(mf.is_fresh, false) and m.temp_c is not null then 'metar'
         else coalesce(e.temp_source, case when m.temp_c is not null then 'metar' end)
    end                                                                    as temp_source,
    case when coalesce(mf.is_fresh, false) and m.dewpoint_c is not null then m.dewpoint_c
         else coalesce(e.dewpoint_c, m.dewpoint_c) end                     as dewpoint_c,
    case when coalesce(mf.is_fresh, false) and m.dewpoint_c is not null then 'metar'
         else coalesce(e.dewpoint_source, case when m.dewpoint_c is not null then 'metar' end)
    end                                                                    as dewpoint_source,
    -- Today's sunrise and sunset, local time at the airport (HH:MM).
    strftime(sun.sunrise at time zone a.timezone, '%H:%M')                 as sunrise_local,
    strftime(sun.sunset at time zone a.timezone, '%H:%M')                  as sunset_local,
    sun.source                                                             as sun_source,
    m.altimeter_hpa,
    pl.name                       as procedures_name,
    pl.url                        as procedures_url,
    pl.kind                       as procedures_kind,
    atis.arrival_letter           as atis_arrival_letter,
    atis.departure_letter         as atis_departure_letter,
    atis.text                     as atis_text,
    atis.last_seen_at             as atis_seen_at,
    t.issued_at                   as taf_issued_at,
    t.valid_from                  as taf_valid_from,
    t.valid_to                    as taf_valid_to,
    t.raw_text                    as taf_raw,
    -- Null, not 0, where the airport has no NOTAM feed.
    case when f.source is not null then coalesce(n.n, 0) end as notams_in_force
from {{ ref('airports') }} a
left join latest_metar m on m.icao = a.icao
left join (select icao, observed_at >= now() - interval 2 hour as is_fresh from latest_metar) mf
    on mf.icao = a.icao
left join extra e on e.icao = a.icao
left join {{ source('raw', 'sun_times') }} sun
    on sun.icao = a.icao and sun.day = cast(now() at time zone a.timezone as date)
left join {{ source('raw', 'procedure_links') }} pl on pl.icao = a.icao
left join latest_atis atis on atis.icao = a.icao
left join latest_taf t on t.icao = a.icao
left join notams n on n.location = a.icao
left join notam_feeds f on f.source = a.notam_source
