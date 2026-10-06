"""Parsers and sun times for sources/wx_extra.py.

The payloads are written from each provider's documented response shape (the hosts are
not reachable from the test sandbox), so they pin the parsing, not the live APIs.
"""
from datetime import date, datetime, timezone

import pytest

from aviation.sources import wx_extra as wx


def test_hko_takes_the_airport_place_and_has_no_dew_point():
    payload = {"temperature": {"recordTime": "2026-10-06T18:00:00+08:00", "data": [
        {"place": "King's Park", "value": 24, "unit": "C"},
        {"place": "Chek Lap Kok", "value": 26, "unit": "C"}]}}
    got = wx.parse_hko(payload, "Chek Lap Kok")
    assert got["temp_c"] == 26 and got["dewpoint_c"] is None
    assert wx.parse_hko(payload, " chek lap kok ")["temp_c"] == 26, "case and spacing ignored"
    assert wx.parse_hko(payload, "Chep Lap Kok") is None and wx.parse_hko(payload, None) is None
    assert wx.hko_places(payload) == ["King's Park", "Chek Lap Kok"]
    assert got["observed_at"] == datetime(2026, 10, 6, 10, tzinfo=timezone.utc)
    assert wx.parse_hko({"temperature": {"data": [{"place": "King's Park", "value": 24}]}},
                        "Chek Lap Kok") is None


def test_hko_logs_the_places_it_lists_when_the_station_name_is_wrong(monkeypatch, caplog):
    payload = {"temperature": {"data": [{"place": "King's Park", "value": 24},
                                        {"place": "Chek Lap Kok", "value": 26}]}}
    monkeypatch.setattr(wx, "_get_json", lambda *a, **k: payload)
    with caplog.at_level("WARNING"):
        assert wx.fetch_gov("hko", "VHHH", 22.3, 113.9, "Chep Lap Kok") is None
    assert "Chep Lap Kok" in caplog.text and "King's Park, Chek Lap Kok" in caplog.text
    assert wx.fetch_gov("hko", "VHHH", 22.3, 113.9, "Chek Lap Kok")[1]["temp_c"] == 26


def test_config_names_the_hko_station():
    from aviation.config import load_settings
    assert load_settings().weather_stations["VHHH"] == "Chek Lap Kok"


def test_nea_picks_the_nearest_station_with_a_reading():
    payload = {"data": {
        "stations": [
            {"id": "S1", "location": {"latitude": 1.36, "longitude": 103.99}},
            {"id": "S2", "location": {"latitude": 1.30, "longitude": 103.80}},
            {"id": "S3", "location": {"latitude": 1.359, "longitude": 103.989}}],
        "readings": [
            {"timestamp": "2026-10-06T09:00:00+08:00", "data": [{"stationId": "S2", "value": 20}]},
            {"timestamp": "2026-10-06T10:00:00+08:00", "data": [
                {"stationId": "S2", "value": 29.0}, {"stationId": "S1", "value": 31.5},
                {"stationId": "S3", "value": None}]}]}}
    got = wx.parse_nea(payload, 1.359, 103.989)
    assert got["temp_c"] == 31.5, "S3 is closest but has no value; S1 is next"
    assert wx.parse_nea({"data": {"stations": [], "readings": []}}, 1.0, 103.0) is None


def test_nws_reads_temperature_dew_point_and_present_weather():
    payload = {"properties": {
        "timestamp": "2026-10-06T10:53:00+00:00", "textDescription": "Light Rain",
        "temperature": {"value": 4.4}, "dewpoint": {"value": 3.9},
        "presentWeather": [{"weather": "light rain"}, {"weather": "mist"}]}}
    got = wx.parse_nws(payload)
    assert (got["temp_c"], got["dewpoint_c"], got["wx_text"]) == (4.4, 3.9, "light rain, mist")
    assert wx.parse_nws({"properties": {"temperature": {"value": None}, "dewpoint": {}}}) is None


def test_open_meteo_maps_the_wmo_code():
    got = wx.parse_open_meteo({"current": {"time": "2026-10-06T10:15", "temperature_2m": 11.2,
                                           "dew_point_2m": 8.0, "weather_code": 63}})
    assert (got["temp_c"], got["dewpoint_c"], got["wx_text"]) == (11.2, 8.0, "Rain")
    assert got["observed_at"] == datetime(2026, 10, 6, 10, 15, tzinfo=timezone.utc)
    assert wx.parse_open_meteo({"current": {}}) is None


def test_met_no_uses_the_first_hour_and_drops_the_day_night_suffix():
    payload = {"properties": {"timeseries": [{"time": "2026-10-06T11:00:00Z", "data": {
        "instant": {"details": {"air_temperature": 9.5, "dew_point_temperature": 7.1}},
        "next_1_hours": {"summary": {"symbol_code": "lightrainshowers_day"}}}}]}}
    got = wx.parse_met_no(payload)
    assert (got["temp_c"], got["dewpoint_c"], got["wx_text"]) == (9.5, 7.1, "Lightrainshowers")
    assert wx.parse_met_no({"properties": {"timeseries": []}}) is None


def test_met_no_sun_reads_sunrise_and_sunset():
    rise, sset = wx.parse_met_no_sun({"properties": {
        "sunrise": {"time": "2026-10-06T06:30:00+11:00"},
        "sunset": {"time": "2026-10-06T18:55:00+11:00"}}})
    assert rise == datetime(2026, 10, 5, 19, 30, tzinfo=timezone.utc)
    assert sset == datetime(2026, 10, 6, 7, 55, tzinfo=timezone.utc)


@pytest.mark.parametrize("day, lat, lon, minutes_utc", [
    # Equinox on the equator at Greenwich: about 12 h of daylight, sunrise near 06:07 UTC.
    (date(2026, 3, 20), 0.0, 0.0, (6 * 60 + 7, 18 * 60 + 7)),
])
def test_computed_sun_times_match_known_geometry(day, lat, lon, minutes_utc):
    rise, sset = wx.compute_sun(day, lat, lon)
    got = (rise.hour * 60 + rise.minute, sset.hour * 60 + sset.minute)
    assert all(abs(a - b) <= 6 for a, b in zip(got, minutes_utc))


def test_computed_sun_day_length_and_polar_cases():
    rise, sset = wx.compute_sun(date(2026, 12, 21), -33.946, 151.177)  # Sydney, midsummer
    hours = (sset - rise).total_seconds() / 3600
    assert 14.2 < hours < 14.6
    rise, sset = wx.compute_sun(date(2026, 6, 21), 61.179, -149.993)  # Anchorage, midsummer
    assert 19 < (sset - rise).total_seconds() / 3600 < 20
    assert wx.compute_sun(date(2026, 12, 21), 78.2, 15.6) == (None, None)  # Svalbard, polar night
