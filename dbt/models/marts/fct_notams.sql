-- NOTAMs with the flags needed to line them up against arrivals.
select
    n.*,
    c.iata                                                      as location_iata,
    a.icao is not null                                          as is_in_scope_aerodrome,
    n.category in ('runway', 'ils', 'taxiway', 'movement_area') as is_surface_or_approach,
    n.category = 'runway' and n.condition = 'closed'            as is_runway_closure,
    -- An estimated end (C) ... EST) does not expire a NOTAM: it stays in force until it is
    -- cancelled or replaced, i.e. until it drops out of the feed.
    n.in_latest_feed
      and n.starts_at <= now()
      and (n.ends_at is null or n.ends_at > now() or n.is_estimated) as is_current
from {{ ref('stg_notam') }} n
left join {{ ref('airports') }} a on a.icao = n.location
left join {{ ref('airport_codes') }} c on c.icao = n.location
