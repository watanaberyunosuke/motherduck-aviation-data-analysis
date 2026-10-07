-- Hong Kong's flight boards (sources/schedule_hk.py), one row per flight and scheduled time.
-- gate_at is off-block for departures ("Dep"), on-block for arrivals ("At gate"); null
-- until the board shows it, and for cancellations.
select
    'VHHH'        as airport_icao,
    direction,
    flight,
    scheduled_at,
    callsign,
    is_cargo,
    status,
    gate_at,
    board_date,
    fetched_at
from {{ source('raw', 'hkia_flights') }}
