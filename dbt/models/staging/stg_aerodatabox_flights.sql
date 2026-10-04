-- One row per flight from AeroDataBox airport boards (FIDS), with its schedule.
-- A flight between two in-scope airports is on both boards; each side is taken from its
-- own airport's board where loaded (it carries that airport's live times), otherwise from
-- the other board's copy of the leg. Times are runway, else revised (actual or
-- estimated), else scheduled; `time_is_scheduled` marks the last case.
with boards as (
    select
        *,
        payload -> 'departure' as dep,
        payload -> 'arrival'   as arr,
        epoch(fetched_at)      as fetched_epoch
    from {{ source('raw', 'aerodatabox_flights') }}
),

merged as (
    select
        flight_id,
        arg_max(dep, (direction = 'departure')::int * 1e10 + fetched_epoch) as dep,
        arg_max(arr, (direction = 'arrival')::int * 1e10 + fetched_epoch)   as arr,
        arg_max(payload, fetched_epoch)                                      as payload,
        arg_max(status, fetched_epoch)                                       as status,
        max(icao24)                                                          as icao24,
        max(callsign)                                                        as callsign_raw
    from boards
    group by flight_id
),

times as (
    select
        *,
        {{ adb_time('dep', 'scheduledTime') }} as scheduled_departure_at,
        {{ adb_time('arr', 'scheduledTime') }} as scheduled_arrival_at,
        coalesce({{ adb_time('dep', 'runwayTime') }}, {{ adb_time('dep', 'revisedTime') }}) as actual_departure_at,
        coalesce({{ adb_time('arr', 'runwayTime') }}, {{ adb_time('arr', 'revisedTime') }}) as actual_arrival_at,
        nullif(replace(payload ->> 'number', ' ', ''), '')                 as flight_number,
        (payload -> 'airline') ->> 'icao'                                  as airline_icao,
        (dep -> 'airport') ->> 'icao'                                      as departure_icao,
        (dep -> 'airport') ->> 'iata'                                      as departure_iata_raw,
        (arr -> 'airport') ->> 'icao'                                      as arrival_icao,
        (arr -> 'airport') ->> 'iata'                                      as arrival_iata_raw
    from merged
)

select
    t.flight_id,
    t.icao24,
    -- Boards usually give the ATC callsign (QFA627); otherwise build it from the airline's
    -- ICAO designator and the flight number, as an airline would file it.
    coalesce(nullif(trim(t.callsign_raw), ''),
             t.airline_icao || nullif(regexp_extract(t.flight_number, '([0-9]+)$', 1), ''))
                                                                           as callsign,
    t.flight_number                                                        as flight_number_iata,
    coalesce((t.payload -> 'airline') ->> 'name', al.name)                 as airline_name,
    t.departure_icao,
    coalesce(t.departure_iata_raw, dep_codes.iata)                         as departure_iata,
    t.arrival_icao,
    coalesce(t.arrival_iata_raw, arr_codes.iata)                           as arrival_iata,
    t.departure_icao || '-' || t.arrival_icao                              as route,
    coalesce(t.actual_departure_at, t.scheduled_departure_at)              as departed_at,
    coalesce(t.actual_arrival_at, t.scheduled_arrival_at)                  as arrived_at,
    t.scheduled_departure_at,
    t.scheduled_arrival_at,
    t.actual_departure_at is null                                          as departure_time_is_scheduled,
    t.actual_arrival_at is null                                            as arrival_time_is_scheduled,
    t.status,
    coalesce(t.status in ('Canceled', 'CanceledUncertain', 'Diverted'), false) as is_cancelled,
    t.dep ->> 'runway'                                                     as departure_runway,
    t.arr ->> 'runway'                                                     as arrival_runway,
    (t.payload -> 'aircraft') ->> 'reg'                                    as registration,
    (t.payload -> 'aircraft') ->> 'model'                                  as aircraft_model
from times t
left join {{ ref('airlines') }} al on al.icao = t.airline_icao
left join {{ ref('airport_codes') }} dep_codes on dep_codes.icao = t.departure_icao
left join {{ ref('airport_codes') }} arr_codes on arr_codes.icao = t.arrival_icao
