-- One row per NOTAM per source, with Q-code category and presence in the latest feed.
with src as (
    select * from {{ source('raw', 'notam') }}
),

latest_fetch as (
    select source, max(last_seen_at) as latest_seen_at
    from src
    group by source
)

select
    src.source,
    src.notam_key,
    src.number,
    src.notam_type,
    src.replaces,
    src.location,
    src.fir,
    src.q_code,
    coalesce(subj.category, case when src.q_code is not null then 'other' end) as category,
    coalesce(cond.condition, case when src.q_code is not null then 'other' end) as condition,
    src.traffic,
    src.purpose,
    src.scope,
    src.lower_fl,
    src.upper_fl,
    src.centre_lat,
    src.centre_lon,
    src.radius_nm,
    src.starts_at,
    src.ends_at,
    coalesce(src.is_permanent, false)  as is_permanent,
    coalesce(src.is_estimated, false)  as is_estimated,
    src.schedule,
    -- A D) schedule means the NOTAM is only active in the listed sub-windows, so
    -- [starts_at, ends_at) overstates its active time. Flag it rather than guess.
    src.schedule is not null           as has_schedule,
    src.body,
    src.lower_limit,
    src.upper_limit,
    src.raw_text,
    src.first_seen_at,
    src.last_seen_at,
    src.last_seen_at = lf.latest_seen_at as in_latest_feed
from src
left join latest_fetch lf using (source)
left join {{ ref('notam_q_subjects') }} subj on subj.subject_code = substr(src.q_code, 2, 2)
left join {{ ref('notam_q_conditions') }} cond on cond.condition_code = substr(src.q_code, 4, 2)
