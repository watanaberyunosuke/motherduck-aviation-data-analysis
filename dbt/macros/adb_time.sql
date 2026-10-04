{# UTC time of an AeroDataBox movement field (scheduledTime, revisedTime, runwayTime).
   The API writes "2026-10-03 06:35Z"; DuckDB's cast needs seconds, so parse it. #}
{% macro adb_time(movement, field) -%}
    cast(try_strptime(({{ movement }} -> '{{ field }}') ->> 'utc',
                      ['%Y-%m-%d %H:%MZ', '%Y-%m-%d %H:%M:%SZ', '%Y-%m-%d %H:%M%z']) as timestamptz)
{%- endmacro %}
