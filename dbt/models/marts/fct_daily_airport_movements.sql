-- Movements per in-scope airport per UTC day, from stg_flights: what OpenSky's ADS-B
-- receivers saw, which undercounts where coverage is thin, or AeroDataBox's boards
-- (cancelled flights excluded) where OpenSky has nothing.
with movements as (
    select arrival_icao as icao, cast(last_seen_at as date) as day_utc, 'arrival' as direction
    from {{ ref('stg_flights') }}
    where arrival_icao is not null
    union all
    select departure_icao, cast(first_seen_at as date), 'departure'
    from {{ ref('stg_flights') }}
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
