"""Vercel entrypoint for the arrival-weather dashboard.

Self-contained on purpose: it talks to the warehouse directly with its own SQL rather
than importing the `aviation` package, so the deployed function has no dependency on
how `src/` gets bundled. Reads only `marts.*` -- never writes.
"""
from __future__ import annotations

import os

import duckdb
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

_con: duckdb.DuckDBPyConnection | None = None


def get_connection() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        target = os.environ.get("WAREHOUSE", "md:aviation")
        _con = duckdb.connect(target)
    return _con


def query(sql: str) -> list[dict]:
    """Run a read query against the marts; empty list if the pipeline hasn't built them yet."""
    try:
        con = get_connection()
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except duckdb.Error as exc:
        print(f"dashboard query failed: {exc}")
        return []


@app.get("/api/summary")
def summary() -> JSONResponse:
    rows = query(
        """
        select
            arrival_icao,
            flight_category,
            avg(excess_terminal_minutes) as avg_excess_minutes,
            count(*) as n
        from marts.fct_arrival_weather_impact
        where flight_category is not null
        group by arrival_icao, flight_category
        order by arrival_icao, flight_category
        """
    )
    return JSONResponse(rows)


@app.get("/api/notams")
def notams() -> JSONResponse:
    rows = query(
        """
        select
            source, notam_key, number, location, category, condition,
            centre_lat, centre_lon, radius_nm,
            starts_at::varchar as starts_at, ends_at::varchar as ends_at, body
        from marts.fct_notams
        where is_current
          and centre_lat is not null
          and centre_lon is not null
        order by starts_at desc
        limit 500
        """
    )
    return JSONResponse(rows)


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return _PAGE


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aviation weather impact</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.5.1/chart.umd.min.js"
        integrity="sha512-WoViKhKD4qI2WruSZqv9+kvM4WfFhUMQCLN4QlDTt5aU56fLQy2gYoxWIqlEnXqJy/+Ac5q/hk1oWfqnMDhwMA=="
        crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"
      integrity="sha512-h9FcoyWjHcOcmEVkxOfTLnmZFWIH0iZhZT1H2TbOq55xssQGEJHEaIm+PgoUaZbRvQTNTluNOEfb1ZRy6D3BOw=="
      crossorigin="anonymous" referrerpolicy="no-referrer">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"
        integrity="sha512-puJW3E/qXDqYp9IfhAI54BJEaWIfloJ7JWs7OeD5i6ruC9JZL1gERT1wjtwXFlh7CjE7ZJ+/vcRZRkIYIb6p4g=="
        crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 24px;
         background: #0b0d12; color: #e7e9ee; }
  h1 { font-size: 1.3rem; margin: 0 0 4px; }
  p.sub { color: #9aa3b2; margin: 0 0 24px; font-size: 0.9rem; }
  .card { background: #151821; border: 1px solid #262b38; border-radius: 10px;
          padding: 16px; margin-bottom: 24px; }
  .card h2 { margin: 0 0 12px; font-size: 1rem; }
  .empty { color: #9aa3b2; font-size: 0.9rem; padding: 12px 0; }
  #map { height: 420px; border-radius: 8px; }
  canvas { max-height: 360px; }
</style>
</head>
<body>
  <h1>Excess terminal-area time and live NOTAMs</h1>
  <p class="sub">Minutes an arrival spends within 50 NM of its destination beyond that airport's rolling median, by flight category.</p>

  <div class="card">
    <h2>Excess terminal minutes by airport</h2>
    <div id="chart-empty" class="empty" style="display:none">
      No data yet. The ingestion + dbt pipeline hasn't populated the warehouse marts --
      trigger the <code>ingest</code> workflow or run <code>make all</code>, then refresh.
    </div>
    <canvas id="summaryChart"></canvas>
  </div>

  <div class="card">
    <h2>NOTAMs currently in force</h2>
    <div id="map-empty" class="empty" style="display:none">
      No current NOTAMs with coordinates (Hong Kong only has a NOTAM feed for now).
    </div>
    <div id="map"></div>
  </div>

<script>
const CATEGORY_COLORS = {
  VFR: '#4ade80', MVFR: '#60a5fa', IFR: '#f97316', LIFR: '#f87171'
};
const CATEGORY_ORDER = ['VFR', 'MVFR', 'IFR', 'LIFR'];

async function loadSummary() {
  const rows = await fetch('/api/summary').then(r => r.json());
  if (!rows.length) {
    document.getElementById('chart-empty').style.display = 'block';
    return;
  }
  const airports = [...new Set(rows.map(r => r.arrival_icao))].sort();
  const datasets = CATEGORY_ORDER.filter(cat => rows.some(r => r.flight_category === cat))
    .map(cat => ({
      label: cat,
      backgroundColor: CATEGORY_COLORS[cat] || '#999',
      data: airports.map(a => {
        const match = rows.find(r => r.arrival_icao === a && r.flight_category === cat);
        return match ? Number(match.avg_excess_minutes) : null;
      }),
    }));
  new Chart(document.getElementById('summaryChart'), {
    type: 'bar',
    data: { labels: airports, datasets },
    options: {
      responsive: true,
      scales: {
        x: { ticks: { color: '#e7e9ee' }, grid: { color: '#262b38' } },
        y: { ticks: { color: '#e7e9ee' }, grid: { color: '#262b38' },
             title: { display: true, text: 'avg excess minutes', color: '#9aa3b2' } },
      },
      plugins: { legend: { labels: { color: '#e7e9ee' } } },
    },
  });
}

async function loadNotams() {
  const rows = await fetch('/api/notams').then(r => r.json());
  const map = L.map('map').setView([5, 110], 3);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; OpenStreetMap, &copy; CARTO',
  }).addTo(map);
  if (!rows.length) {
    document.getElementById('map-empty').style.display = 'block';
    return;
  }
  const bounds = [];
  for (const n of rows) {
    const marker = L.circleMarker([n.centre_lat, n.centre_lon], {
      radius: 6, color: '#f97316', fillColor: '#f97316', fillOpacity: 0.7,
    }).addTo(map);
    marker.bindPopup(
      `<b>${n.number ?? n.notam_key}</b> (${n.location ?? ''})<br>` +
      `${n.category ?? ''} / ${n.condition ?? ''}<br>${n.body ?? ''}`
    );
    bounds.push([n.centre_lat, n.centre_lon]);
  }
  map.fitBounds(bounds, { maxZoom: 8, padding: [20, 20] });
}

loadSummary();
loadNotams();
</script>
</body>
</html>
"""
