-- Observed ADS-B movements per in-scope airport per UTC day. Counts are what OpenSky
-- saw, not the full schedule: they undercount where receiver coverage is thin.
with movements as (
    select arrival_icao as icao, cast(last_seen_at as date) as day_utc, 'arrival' as direction
    from {{ ref('stg_opensky_flights') }}
    where arrival_icao is not null
    union all
    select departure_icao, cast(first_seen_at as date), 'departure'
    from {{ ref('stg_opensky_flights') }}
    where departure_icao is not null
)

select
    m.icao,
    a.iata,
    m.day_utc,
    count(*) filter (where direction = 'arrival')   as arrivals,
    count(*) filter (where direction = 'departure') as departures
from movements m
join {{ ref('airports') }} a on a.icao = m.icao
group by 1, 2, 3
