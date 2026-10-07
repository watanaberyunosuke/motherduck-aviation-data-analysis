{# True when the callsign's ICAO airline designator (its first three letters, then at
   least one more character) is an all-cargo operator in the cargo_operators seed. The API
   tags live aircraft and schedule rows the same way (api/index.py `is_freighter`). #}
{% macro is_freighter(callsign) -%}
    coalesce(regexp_extract({{ callsign }}, '^([A-Z]{3}).', 1)
             in (select icao from {{ ref('cargo_operators') }}), false)
{%- endmacro %}
