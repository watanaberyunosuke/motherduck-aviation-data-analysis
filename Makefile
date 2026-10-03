.PHONY: setup ingest-weather ingest-notams ingest-flights ingest-all transform test all

# Pick up WAREHOUSE / MOTHERDUCK_TOKEN from .env and hand dbt an absolute path,
# because dbt runs from the dbt/ directory.
-include .env
export
WAREHOUSE ?= data/aviation.duckdb
ifeq ($(filter md:%,$(WAREHOUSE)),)
  DBT_WAREHOUSE := $(abspath $(WAREHOUSE))
else
  DBT_WAREHOUSE := $(WAREHOUSE)
endif

setup:
	pip install -e ".[dev]"

ingest-weather:
	aviation ingest metar
	aviation ingest taf

ingest-notams:
	aviation ingest notam

ingest-flights:
	aviation ingest opensky

ingest-all: ingest-weather ingest-notams ingest-flights

transform:
	cd dbt && WAREHOUSE=$(DBT_WAREHOUSE) dbt build --profiles-dir .

test:
	pytest -q

all: ingest-all transform
