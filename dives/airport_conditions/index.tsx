// MotherDuck Dive: per-airport weather, traffic, arrival and departure impact.
// Reads aviation.marts / aviation.reference only. Airports and flights are shown with
// IATA codes (SYD, QF627); the marts keep the ICAO forms too, and filters use ICAO.
// Flight times are shown in the airport's local time (reference.airports.timezone).
// Published by scripts/deploy_motherduck.py; preview locally with `motherduck dive watch`.
// The same file is the Vercel site: web/ bundles it and runs its SQL on DuckDB-WASM.
// Live aircraft come from the Vercel API (/api/live), which proxies OpenSky.
import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { useSQLQuery, useDiveState } from "@motherduck/react-sql-query";
import {
  Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

export const REQUIRED_DATABASES = [{ type: "database", path: "md:aviation", alias: "aviation" }];

const N = (v: unknown): number => (v != null ? Number(v) : 0);
const rowsOf = (data: unknown): Record<string, any>[] => (Array.isArray(data) ? data : []);
const pct = (v: unknown) => (v == null ? "–" : `${Math.round(N(v) * 100)}%`);
const mins = (v: unknown) => (v == null ? "–" : `${N(v) >= 0 ? "+" : ""}${N(v).toFixed(1)} min`);

const SANS = "'Inter', system-ui, -apple-system, sans-serif";
const INK = "#1a1a1a";
const MUTED = "#6a6a6a";
const RULE = "#e5e5e5";
const BLUE = "#2563eb";
const ORANGE = "#ea580c";
// Standard aviation flight-category colours.
const CATEGORY_COLORS: Record<string, string> = {
  VFR: "#16a34a", MVFR: "#2563eb", IFR: "#dc2626", LIFR: "#c026d3",
};

function Skeleton({ h = 200 }: { h?: number }) {
  return <div className="animate-pulse rounded" style={{ height: h, background: RULE }} />;
}

function Empty({ children }: { children: ReactNode }) {
  return <div style={{ color: MUTED, fontSize: 13, padding: "24px 0" }}>{children}</div>;
}

function Section({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <section style={{ marginTop: 32 }}>
      <h2 style={{ fontSize: 15, fontWeight: 600, color: INK, margin: 0 }}>{title}</h2>
      {note && <p style={{ fontSize: 12, color: MUTED, margin: "4px 0 12px" }}>{note}</p>}
      {children}
    </section>
  );
}

function KPI({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ fontSize: 28, fontWeight: 600, color: INK }}>{value}</div>
      <div style={{ fontSize: 12, color: MUTED, marginTop: 2 }}>{label}</div>
    </div>
  );
}

const DEFAULT_AIRPORT = "HKG";
const HOME_TZ = "Australia/Melbourne";
// On the Vercel site the API is same-origin; inside MotherDuck it is the production site.
const API_BASE = /(^|\.)vercel\.app$|^localhost$|^127\.0\.0\.1$/.test(window.location.hostname)
  ? "" : "https://motherduck-aviation-data-analysis.vercel.app";
const LIVE_REFRESH_MS = 120_000; // matches the API's edge cache

// "09:41:07" plus "Sat 4 Oct" and "GMT+8" in the given zone.
function clockParts(now: Date, timeZone: string) {
  const f = (o: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("en-GB", { timeZone, ...o }).format(now);
  const offset = new Intl.DateTimeFormat("en-GB", { timeZone, timeZoneName: "shortOffset" })
    .formatToParts(now).find((p) => p.type === "timeZoneName")?.value ?? "";
  return {
    time: f({ hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }),
    date: f({ weekday: "short", day: "numeric", month: "short" }),
    offset: offset === "GMT" ? "UTC" : offset.replace("GMT", "UTC"),
  };
}

function Clocks({ zones }: { zones: { label: string; tz: string }[] }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 12, marginTop: 20 }}>
      {zones.map(({ label, tz }) => {
        const c = clockParts(now, tz);
        return (
          <div key={label} style={{ border: `1px solid ${RULE}`, borderRadius: 8, padding: "10px 14px" }}>
            <div style={{ fontSize: 12, color: MUTED }}>{label}</div>
            <div style={{ fontSize: 24, fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>{c.time}</div>
            <div style={{ fontSize: 12, color: MUTED }}>{c.date} · {c.offset}</div>
          </div>
        );
      })}
    </div>
  );
}

function Tile({ label, value, color }: { label: string; value: ReactNode; color?: string }) {
  return (
    <div style={{ border: `1px solid ${RULE}`, borderRadius: 8, padding: "8px 12px" }}>
      <div style={{ fontSize: 12, color: MUTED }}>{label}</div>
      <div style={{ fontSize: 17, fontWeight: 600, color: color ?? INK, marginTop: 2 }}>{value}</div>
    </div>
  );
}

const mono: CSSProperties = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12, background: "#f6f7f9",
  border: `1px solid ${RULE}`, borderRadius: 6, padding: "8px 10px", whiteSpace: "pre-wrap", margin: "4px 0 0",
};

const windText = (r: Record<string, any>) =>
  r.wind_speed_kt == null ? "–"
    : N(r.wind_speed_kt) === 0 ? "Calm"
    : `${r.wind_variable ? "VRB" : `${String(N(r.wind_dir_deg)).padStart(3, "0")}°`} ${N(r.wind_speed_kt)} kt`
      + (r.wind_gust_kt != null ? ` G${N(r.wind_gust_kt)}` : "");
// AWC reports visibility in statute miles; "6+" (lower bound) is 10 km or more in ICAO terms.
const visText = (r: Record<string, any>) =>
  r.visibility_sm == null ? "–" : r.visibility_is_lower_bound ? "10 km or more"
    : `${(N(r.visibility_sm) * 1.609).toFixed(1)} km`;

// ---- Map: Web Mercator tiles (Esri) and SVG overlays, no map library ---------------------
const TILE = 256;
const MAP_W = 1000;
const MAP_H = 560;
const TERMINAL_KM = 92.6; // 50 NM, as in dbt_project.yml

function mercator(lat: number, lon: number, z: number): [number, number] {
  const w = TILE * 2 ** z;
  const s = Math.sin((lat * Math.PI) / 180);
  return [((lon + 180) / 360) * w, (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * w];
}

type Live = { icao24: string; callsign: string | null; lat: number; lon: number; alt_ft: number;
  on_ground: boolean; speed_kt: number | null; track_deg: number | null; vrate_fpm: number | null };

// Live positions around the airport, refreshed every 2 minutes.
function useLiveAircraft(icao: string) {
  const [state, setState] = useState<{ aircraft: Live[]; at: Date | null; error: string | null }>(
    { aircraft: [], at: null, error: null });
  useEffect(() => {
    if (!icao) return;
    let stale = false;
    const load = () => fetch(`${API_BASE}/api/live/${icao}`)
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body?.detail ?? `HTTP ${r.status}`);
        return body;
      })
      .then((b) => !stale && setState({ aircraft: b.aircraft ?? [], at: new Date(), error: null }))
      .catch((e) => !stale && setState((s) => ({ ...s, error: String(e?.message ?? e) })));
    setState({ aircraft: [], at: null, error: null });
    load();
    const id = setInterval(load, LIVE_REFRESH_MS);
    return () => { stale = true; clearInterval(id); };
  }, [icao]);
  return state;
}

type TrackLine = { key: string; role: string; label: string; points: [number, number][] };

function AirspaceMap({ lat, lon, wx, tracks, live, flightNumber }: {
  lat: number; lon: number; wx: Record<string, any> | undefined; tracks: TrackLine[];
  live: Live[]; flightNumber: (callsign: string | null) => string;
}) {
  const [zoom, setZoom] = useState(8);
  const [cx, cy] = mercator(lat, lon, zoom);
  const x0 = cx - MAP_W / 2;
  const y0 = cy - MAP_H / 2;
  const xy = (la: number, lo: number): [number, number] => {
    const [x, y] = mercator(la, lo, zoom);
    return [x - x0, y - y0];
  };
  const n = 2 ** zoom;
  const tiles: { key: string; href: string; x: number; y: number }[] = [];
  for (let tx = Math.floor(x0 / TILE); tx <= Math.floor((x0 + MAP_W) / TILE); tx++) {
    for (let ty = Math.floor(y0 / TILE); ty <= Math.floor((y0 + MAP_H) / TILE); ty++) {
      if (ty < 0 || ty >= n) continue;
      const wxTile = ((tx % n) + n) % n;
      tiles.push({
        key: `${zoom}/${tx}/${ty}`,
        // Esri's light grey canvas needs no API key (CARTO's basemaps now do).
        href: `https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/${zoom}/${ty}/${wxTile}`,
        x: tx * TILE - x0, y: ty * TILE - y0,
      });
    }
  }
  const pxPerKm = (TILE * n) / (40075.017 * Math.cos((lat * Math.PI) / 180));
  const catColor = wx?.flight_category ? CATEGORY_COLORS[wx.flight_category] : MUTED;
  const windFrom = wx && !wx.wind_variable && wx.wind_dir_deg != null && N(wx.wind_speed_kt) > 0
    ? (N(wx.wind_dir_deg) * Math.PI) / 180 : null;
  const btn: CSSProperties = {
    width: 28, height: 28, border: `1px solid ${RULE}`, background: "#fff", borderRadius: 6,
    fontSize: 16, lineHeight: "24px", cursor: "pointer", color: INK,
  };

  return (
    <div style={{ position: "relative", border: `1px solid ${RULE}`, borderRadius: 8, overflow: "hidden" }}>
      <svg viewBox={`0 0 ${MAP_W} ${MAP_H}`} style={{ width: "100%", height: "auto", display: "block", background: "#eef1f4" }}>
        <defs>
          <marker id="wind-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0,0 L10,5 L0,10 z" fill={INK} />
          </marker>
        </defs>
        {/* Half a pixel of overlap hides anti-aliasing seams between tiles. */}
        {tiles.map((t) => <image key={t.key} href={t.href} x={t.x} y={t.y} width={TILE + 0.5} height={TILE + 0.5} />)}
        <circle cx={MAP_W / 2} cy={MAP_H / 2} r={TERMINAL_KM * pxPerKm} fill="none" stroke={INK} strokeOpacity={0.35} strokeDasharray="6 5" />
        {tracks.map((t) => (
          <polyline
            key={t.key}
            points={t.points.map(([la, lo]) => xy(la, lo).join(",")).join(" ")}
            fill="none" stroke={t.role === "arrival" ? BLUE : ORANGE} strokeOpacity={0.45} strokeWidth={1.6}
          >
            <title>{t.label}</title>
          </polyline>
        ))}
        {live.map((a) => {
          const [x, y] = xy(a.lat, a.lon);
          if (x < -10 || y < -10 || x > MAP_W + 10 || y > MAP_H + 10) return null;
          const label = flightNumber(a.callsign) || a.icao24;
          return (
            <g key={a.icao24} transform={`translate(${x.toFixed(1)},${y.toFixed(1)})`}>
              <path d="M0,-8 L5.5,6 L0,3 L-5.5,6 Z" transform={`rotate(${N(a.track_deg)})`}
                fill={a.on_ground ? "#9ca3af" : INK} stroke="#fff" strokeWidth={0.8} />
              {zoom >= 9 && <text x={8} y={4} fontSize={10} fill={INK}>{label}</text>}
              <title>
                {`${label}${a.callsign && label !== a.callsign ? ` (${a.callsign})` : ""}\n`
                  + (a.on_ground ? "On ground" : `${a.alt_ft.toLocaleString()} ft`)
                  + (a.speed_kt != null ? `, ${a.speed_kt} kt` : "")
                  + (a.vrate_fpm ? `, ${a.vrate_fpm > 0 ? "+" : ""}${a.vrate_fpm} ft/min` : "")}
              </title>
            </g>
          );
        })}
        {windFrom != null && (
          <line
            x1={MAP_W / 2 + 70 * Math.sin(windFrom)} y1={MAP_H / 2 - 70 * Math.cos(windFrom)}
            x2={MAP_W / 2 + 16 * Math.sin(windFrom)} y2={MAP_H / 2 - 16 * Math.cos(windFrom)}
            stroke={INK} strokeWidth={2.5} markerEnd="url(#wind-head)"
          >
            <title>{`Wind ${windText(wx!)}`}</title>
          </line>
        )}
        <circle cx={MAP_W / 2} cy={MAP_H / 2} r={9} fill={catColor} stroke="#fff" strokeWidth={2.5}>
          <title>{`${wx?.flight_category ?? "No current category"} · ${wx ? windText(wx) : ""}`}</title>
        </circle>
      </svg>
      <div style={{ position: "absolute", top: 10, left: 10, display: "flex", flexDirection: "column", gap: 4 }}>
        <button style={btn} onClick={() => setZoom((z) => Math.min(11, z + 1))} aria-label="Zoom in">+</button>
        <button style={btn} onClick={() => setZoom((z) => Math.max(6, z - 1))} aria-label="Zoom out">−</button>
      </div>
      <div style={{ position: "absolute", right: 6, bottom: 4, fontSize: 10, color: MUTED, background: "rgba(255,255,255,0.8)", padding: "0 4px" }}>
        Esri, HERE, Garmin, © OpenStreetMap contributors
      </div>
    </div>
  );
}

const th: CSSProperties = {
  textAlign: "left", fontWeight: 500, color: MUTED, padding: "6px 12px 6px 0", borderBottom: `1px solid ${RULE}`,
};
const td: CSSProperties = { padding: "6px 12px 6px 0", borderBottom: `1px solid ${RULE}` };
const num: CSSProperties = { ...td, textAlign: "right", fontVariantNumeric: "tabular-nums" };

export default function AirportConditions() {
  // Airports, busiest first.
  const airportsQ = useSQLQuery(`
    select a.icao, a.iata, a.name, a.timezone, count(f.icao24) as arrivals
    from "aviation"."reference"."airports" a
    left join "aviation"."marts"."fct_arrivals" f
      on f.arrival_icao = a.icao and f.arrived_at >= now() - interval 30 day
    group by all
    order by arrivals desc, a.iata
  `);
  const airports = rowsOf(airportsQ.data);
  // The URL holds the IATA code (?airport=SYD); older links with ICAO still resolve.
  const [picked, setPicked] = useDiveState<string>("airport", "");
  const airport = airports.find((a) => a.iata === picked || a.icao === picked)
    ?? airports.find((a) => a.iata === DEFAULT_AIRPORT) ?? airports[0];
  // icao and tz always come from the airports seed, so they are safe to inline.
  const icao = (airport?.icao as string) ?? "";
  const iata = (airport?.iata as string) ?? icao;
  const tz = (airport?.timezone as string) ?? "UTC";
  const ready = { enabled: icao !== "" };

  const overviewQ = useSQLQuery(`
    select
      icao,
      iata,
      count(*)                     as hours,
      avg(is_ifr::int)             as ifr_share,
      avg(is_gusty::int)           as gusty_share,
      avg(has_thunderstorm::int)   as ts_share,
      max(wind_gust_kt)            as max_gust_kt
    from "aviation"."marts"."fct_airport_weather_hourly"
    where hour_utc >= now() - interval 7 day
    group by icao, iata
    order by iata
  `);

  const kpiQ = useSQLQuery(`
    select
      (select count(*) from "aviation"."marts"."fct_arrivals"
       where arrival_icao = '${icao}' and arrived_at >= now() - interval 30 day) as observed,
      count(*)                                        as arrivals,
      median(excess_terminal_minutes)                 as median_excess,
      quantile_cont(excess_terminal_minutes, 0.9)     as p90_excess,
      avg(is_ifr::int) filter (where has_current_metar) as ifr_arrival_share
    from "aviation"."marts"."fct_arrival_weather_impact"
    where arrival_icao = '${icao}'
      and arrived_at >= now() - interval 30 day
  `, ready);

  const conditionsQ = useSQLQuery(`
    select
      *,
      strftime(metar_observed_at at time zone 'UTC', '%d %H:%MZ')   as metar_at,
      date_diff('minute', metar_observed_at, now())                 as metar_age_min,
      strftime(taf_issued_at at time zone 'UTC', '%d %H:%MZ')       as taf_at,
      strftime(taf_valid_from at time zone 'UTC', '%d %H:%MZ')      as taf_from,
      strftime(taf_valid_to at time zone 'UTC', '%d %H:%MZ')        as taf_to
    from "aviation"."marts"."fct_airport_conditions"
    where icao = '${icao}'
  `, ready);

  // Arrival and departure ends of tracked flights, for the map.
  const tracksQ = useSQLQuery(`
    select role, icao24, track_start_epoch, callsign, flight_number_iata, departure_iata,
           arrival_iata, lat, lon
    from "aviation"."marts"."fct_terminal_tracks"
    where airport_icao = '${icao}'
      and point_at >= now() - interval 3 day
    order by role, icao24, track_start_epoch, point_at
  `, ready);

  // ICAO -> IATA airline designators, to label live aircraft by flight number.
  const airlinesQ = useSQLQuery(`select icao, iata from "aviation"."reference"."airlines"`);

  const hourlyQ = useSQLQuery(`
    select
      strftime(hour_utc at time zone '${tz}', '%d %b %H:00') as label,
      flight_category,
      wind_speed_kt,
      wind_gust_kt
    from "aviation"."marts"."fct_airport_weather_hourly"
    where icao = '${icao}'
      and hour_utc >= now() - interval 3 day
    order by hour_utc
  `, ready);

  const movementsQ = useSQLQuery(`
    select strftime(day_utc, '%d %b') as label, arrivals, departures
    from "aviation"."marts"."fct_daily_airport_movements"
    where icao = '${icao}'
      and day_utc >= current_date - 30
    order by day_utc
  `, ready);

  // Median excess terminal time with and without each condition. Only arrivals with a
  // METAR recent enough to describe them; NOTAM rows only where the airport has a feed.
  const penaltyQ = useSQLQuery(`
    with a as (
      select * from "aviation"."marts"."fct_arrival_weather_impact"
      where arrival_icao = '${icao}'
        and excess_terminal_minutes is not null
        and has_current_metar
    ),
    flagged as (
      select 1 as ord, 'IFR or LIFR' as condition, is_ifr as flag, excess_terminal_minutes as x from a
      union all
      select 2, 'Gusts 25 kt or more', coalesce(wind_gust_kt, 0) >= 25, excess_terminal_minutes from a
      union all
      select 3, 'Thunderstorm reported', has_thunderstorm, excess_terminal_minutes from a
      union all
      select 4, 'Runway closure NOTAM', runway_closure_in_force, excess_terminal_minutes from a
    )
    select
      condition,
      count(*) filter (where flag)          as n_with,
      median(x) filter (where flag)         as with_condition,
      count(*) filter (where not flag)      as n_without,
      median(x) filter (where not flag)     as without_condition
    from flagged
    where flag is not null
    group by ord, condition
    order by ord
  `, ready);

  // Every observed arrival on the latest day with data, from any origin. Terminal metrics
  // only exist for the few arrivals whose track was fetched and well covered.
  const arrivalsQ = useSQLQuery(`
    with a as (
      select *, cast(arrived_at at time zone '${tz}' as date) as day_local
      from "aviation"."marts"."fct_arrivals"
      where arrival_icao = '${icao}'
    )
    select
      strftime(day_local, '%d %b %Y')                         as day,
      strftime(arrived_at at time zone '${tz}', '%H:%M')      as arrived,
      coalesce(flight_number_iata, callsign, icao24)          as flight,
      callsign,
      airline_name,
      coalesce(departure_iata, departure_icao)                as origin,
      departure_icao,
      flight_category,
      terminal_minutes,
      excess_terminal_minutes
    from a
    where day_local = (select max(day_local) from a)
    order by arrived_at
  `, ready);

  const worstQ = useSQLQuery(`
    select
      strftime(arrived_at at time zone '${tz}', '%d %b %H:%M') as arrived,
      coalesce(flight_number_iata, trim(callsign), icao24) as flight,
      coalesce(departure_iata, departure_icao) as origin,
      terminal_minutes,
      excess_terminal_minutes,
      flight_category,
      wind_gust_kt,
      wx_string
    from "aviation"."marts"."fct_arrival_weather_impact"
    where arrival_icao = '${icao}'
      and excess_terminal_minutes is not null
      and arrived_at >= now() - interval 30 day
    order by excess_terminal_minutes desc
    limit 10
  `, ready);

  const depKpiQ = useSQLQuery(`
    select
      count(*)                                         as observed,
      count(departure_terminal_minutes)                as with_time,
      median(departure_terminal_minutes)               as median_minutes,
      median(excess_departure_minutes)                 as median_excess,
      avg((flight_category in ('IFR', 'LIFR'))::int)   as ifr_share
    from "aviation"."marts"."fct_departures"
    where departure_icao = '${icao}'
      and departed_at >= now() - interval 30 day
  `, ready);

  // Every observed departure on the latest local day with data, to any destination.
  const departuresQ = useSQLQuery(`
    with d as (
      select *, cast(departed_at at time zone '${tz}' as date) as day_local
      from "aviation"."marts"."fct_departures"
      where departure_icao = '${icao}'
    )
    select
      strftime(day_local, '%d %b %Y')                         as day,
      strftime(departed_at at time zone '${tz}', '%H:%M')     as departed,
      coalesce(flight_number_iata, callsign, icao24)          as flight,
      callsign,
      airline_name,
      coalesce(arrival_iata, arrival_icao)                    as destination,
      arrival_icao,
      flight_category,
      departure_terminal_minutes,
      excess_departure_minutes
    from d
    where day_local = (select max(day_local) from d)
    order by departed_at
  `, ready);

  const live = useLiveAircraft(icao);
  const airlineIata = useMemo(
    () => new Map(rowsOf(airlinesQ.data).map((r) => [String(r.icao), String(r.iata)])),
    [airlinesQ.data],
  );
  // QFA627 -> QF627, as stg_opensky_flights does; other callsigns are shown as they are.
  const flightNumber = (callsign: string | null) => {
    const m = callsign ? /^([A-Z]{3})0*(\d{1,4})$/.exec(callsign) : null;
    return m && airlineIata.has(m[1]) ? `${airlineIata.get(m[1])}${m[2]}` : callsign ?? "";
  };
  const tracks = useMemo(() => {
    const lines = new Map<string, TrackLine>();
    for (const r of rowsOf(tracksQ.data)) {
      const key = `${r.role}|${r.icao24}|${r.track_start_epoch}`;
      if (!lines.has(key)) {
        const flight = r.flight_number_iata ?? r.callsign ?? r.icao24;
        lines.set(key, { key, role: r.role, points: [],
          label: `${flight} ${r.departure_iata ?? "?"}→${r.arrival_iata ?? "?"} (${r.role})` });
      }
      lines.get(key)!.points.push([N(r.lat), N(r.lon)]);
    }
    return [...lines.values()];
  }, [tracksQ.data]);
  const wx = rowsOf(conditionsQ.data)[0];

  const hourly = useMemo(
    () => rowsOf(hourlyQ.data).map((r) => ({
      label: String(r.label),
      category: r.flight_category as string | null,
      wind: r.wind_speed_kt == null ? null : N(r.wind_speed_kt),
      gust: r.wind_gust_kt == null ? null : N(r.wind_gust_kt),
    })),
    [hourlyQ.data],
  );
  const movements = useMemo(
    () => rowsOf(movementsQ.data).map((r) => ({
      label: String(r.label), arrivals: N(r.arrivals), departures: N(r.departures),
    })),
    [movementsQ.data],
  );
  const kpi = rowsOf(kpiQ.data)[0];
  const depKpi = rowsOf(depKpiQ.data)[0];
  const name = airport?.name;
  const arrivals = rowsOf(arrivalsQ.data);
  const departures = rowsOf(departuresQ.data);

  const page: CSSProperties = { fontFamily: SANS, color: INK, padding: 24, maxWidth: 1100 };
  const intro = (
    <>
      <h1 style={{ fontSize: 22, fontWeight: 600, margin: 0 }}>Airport conditions</h1>
      <p style={{ fontSize: 13, color: MUTED, margin: "4px 0 0" }}>
        Weather, observed traffic and excess terminal-area time (minutes within 50 NM beyond the
        airport's rolling median). Flight and chart times are the selected airport's local time;
        METAR and TAF times are UTC (Z); daily movement counts use UTC days.
      </p>

      <Clocks zones={[
        { label: "UTC", tz: "UTC" },
        ...(airport ? [{ label: `${iata} local`, tz }] : []),
        { label: "Melbourne", tz: HOME_TZ },
      ]} />

      <Section title="Last 7 days, all airports" note="Share of observed hours. Click an airport to drill in.">
        {overviewQ.isLoading ? <Skeleton h={160} /> : overviewQ.isError ? (
          <Empty>Could not load weather: {String(overviewQ.error?.message ?? overviewQ.error)}</Empty>
        ) : rowsOf(overviewQ.data).length === 0 ? (
          <Empty>No METARs in the last 7 days. Check that the aviation_pipeline Flight is running.</Empty>
        ) : (
          <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
            <thead>
              <tr>
                <th style={th}>Airport</th>
                <th style={{ ...th, textAlign: "right" }}>Hours</th>
                <th style={{ ...th, textAlign: "right" }}>IFR / LIFR</th>
                <th style={{ ...th, textAlign: "right" }}>Gusts ≥ 25 kt</th>
                <th style={{ ...th, textAlign: "right" }}>Thunderstorm</th>
                <th style={{ ...th, textAlign: "right" }}>Max gust</th>
              </tr>
            </thead>
            <tbody>
              {rowsOf(overviewQ.data).map((r) => (
                <tr
                  key={r.icao}
                  onClick={() => setPicked(r.iata)}
                  style={{ cursor: "pointer", background: r.icao === icao ? "#f3f4f6" : undefined }}
                >
                  <td style={{ ...td, fontWeight: r.icao === icao ? 600 : 400 }} title={r.icao}>{r.iata}</td>
                  <td style={num}>{N(r.hours)}</td>
                  <td style={num}>{pct(r.ifr_share)}</td>
                  <td style={num}>{pct(r.gusty_share)}</td>
                  <td style={num}>{pct(r.ts_share)}</td>
                  <td style={num}>{r.max_gust_kt == null ? "–" : `${N(r.max_gust_kt)} kt`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </>
  );

  // Every per-airport section waits for the airport list, so without it they would sit on
  // skeletons forever. Say why instead.
  if (airportsQ.isError || (!airportsQ.isLoading && airports.length === 0)) {
    return (
      <div style={page}>
        {intro}
        <Section title="Airport detail">
          <Empty>
            {airportsQ.isError
              ? `Could not load the airport list: ${String(airportsQ.error?.message ?? airportsQ.error)}`
              : "No airports in reference.airports. Run dbt seed."}
          </Empty>
        </Section>
      </div>
    );
  }

  return (
    <div style={page}>
      {intro}

      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginTop: 40 }}>
        <select
          value={iata}
          onChange={(e) => setPicked(e.target.value)}
          style={{ fontSize: 15, fontWeight: 600, padding: "4px 8px", border: `1px solid ${RULE}`, borderRadius: 6 }}
        >
          {airports.map((a) => (
            <option key={a.icao} value={a.iata}>{a.iata}</option>
          ))}
        </select>
        <span style={{ fontSize: 15, color: MUTED }}>{name}</span>
        <span style={{ fontSize: 12, color: MUTED }}>{icao}</span>
      </div>

      <Section
        title="Current conditions"
        note={wx?.metar_at ? `Latest METAR ${wx.metar_at}, ${N(wx.metar_age_min)} min ago. Refreshed hourly by the pipeline.` : undefined}
      >
        {conditionsQ.isLoading ? <Skeleton h={180} /> : conditionsQ.isError ? (
          <Empty>Could not load conditions: {String(conditionsQ.error?.message ?? conditionsQ.error)}</Empty>
        ) : !wx || wx.metar_raw == null ? (
          <Empty>No METAR for {iata} yet.</Empty>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
              <Tile label="Flight category" value={wx.flight_category ?? "–"}
                color={wx.flight_category ? CATEGORY_COLORS[wx.flight_category] : MUTED} />
              <Tile label="Wind" value={windText(wx)} />
              <Tile label="Visibility" value={visText(wx)} />
              <Tile label="Ceiling" value={wx.ceiling_ft == null ? "None" : `${N(wx.ceiling_ft).toLocaleString()} ft`} />
              <Tile label="Temp / dew point" value={wx.temp_c == null ? "–" : `${N(wx.temp_c)}° / ${wx.dewpoint_c == null ? "–" : N(wx.dewpoint_c)}°C`} />
              <Tile label="QNH" value={wx.altimeter_hpa == null ? "–" : `${Math.round(N(wx.altimeter_hpa))} hPa`} />
              <Tile label="Weather" value={wx.wx_string ?? "Nil"} />
              <Tile label="NOTAMs in force" value={wx.notams_in_force == null ? "No feed" : String(N(wx.notams_in_force))} />
            </div>
            <div style={{ fontSize: 12, color: MUTED, marginTop: 14 }}>METAR</div>
            <pre style={mono}>{wx.metar_raw}</pre>
            <div style={{ fontSize: 12, color: MUTED, marginTop: 10 }}>
              {wx.taf_raw ? `TAF issued ${wx.taf_at}, valid ${wx.taf_from} to ${wx.taf_to}` : "TAF"}
            </div>
            <pre style={mono}>{wx.taf_raw ?? "No TAF for this airport yet."}</pre>
          </>
        )}
      </Section>

      <Section
        title="Airspace"
        note="Live aircraft within about 275 km, observed arrival (blue) and departure (orange) paths of tracked flights over the last 3 days, which trace the arrival and departure procedures in use, and the 50 NM terminal area. The arrow shows the wind."
      >
        {!wx ? <Skeleton h={400} /> : (
          <AirspaceMap
            lat={N(wx.lat)}
            lon={N(wx.lon)}
            wx={wx}
            tracks={tracks}
            live={live.aircraft}
            flightNumber={flightNumber}
          />
        )}
        <p style={{ fontSize: 12, color: MUTED, margin: "8px 0 0" }}>
          {live.error
            ? `Live positions unavailable: ${live.error}.`
            : live.at
              ? `${live.aircraft.length} live aircraft from OpenSky, updated ${clockParts(live.at, tz).time} ${iata} time. Hover an aircraft for its flight, altitude and speed.`
              : "Loading live positions…"}
          {" "}{tracks.length} tracked paths. Official SID/STAR charts are not shown; no free procedure data covers these airports.
        </p>
      </Section>

      <Section title="Arrivals, last 30 days">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 24 }}>
          {kpiQ.isLoading || !kpi ? <Skeleton h={56} /> : (
            <>
              <KPI label="Arrivals observed, 30 days" value={String(N(kpi.observed))} />
              <KPI label="With terminal time, 30 days" value={String(N(kpi.arrivals))} />
              <KPI label="Median excess terminal time" value={mins(kpi.median_excess)} />
              <KPI label="90th percentile excess" value={mins(kpi.p90_excess)} />
              <KPI label="Arrivals in IFR / LIFR" value={pct(kpi.ifr_arrival_share)} />
            </>
          )}
        </div>
      </Section>

      <Section title="Wind and flight category, last 72 hours" note="Each cell in the strip is one hour's latest METAR.">
        {hourlyQ.isLoading ? <Skeleton h={240} /> : hourly.length === 0 ? (
          <Empty>No METARs for {iata} in the last 72 hours.</Empty>
        ) : (
          <>
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={hourly} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
                <CartesianGrid stroke={RULE} vertical={false} />
                <XAxis dataKey="label" tick={{ fontSize: 11, fill: MUTED }} interval="preserveStartEnd" minTickGap={40} />
                <YAxis tick={{ fontSize: 11, fill: MUTED }} unit=" kt" />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Line type="monotone" dataKey="wind" name="Wind" stroke={BLUE} dot={false} strokeWidth={2} connectNulls />
                <Line type="monotone" dataKey="gust" name="Gust" stroke={ORANGE} dot={{ r: 2 }} strokeWidth={0} />
              </LineChart>
            </ResponsiveContainer>
            <div style={{ display: "flex", gap: 1, margin: "4px 8px 0 44px" }}>
              {hourly.map((h) => (
                <div
                  key={h.label}
                  title={`${h.label}: ${h.category ?? "no category"}`}
                  style={{ flex: 1, height: 14, background: h.category ? CATEGORY_COLORS[h.category] : RULE }}
                />
              ))}
            </div>
            <div style={{ display: "flex", gap: 16, fontSize: 11, color: MUTED, marginTop: 8, marginLeft: 44 }}>
              {Object.entries(CATEGORY_COLORS).map(([k, c]) => (
                <span key={k}><span style={{ display: "inline-block", width: 10, height: 10, background: c, marginRight: 4 }} />{k}</span>
              ))}
            </div>
          </>
        )}
      </Section>

      <Section title="Observed movements, last 30 days" note="What OpenSky saw. Undercounts where ADS-B receiver coverage is thin.">
        {movementsQ.isLoading ? <Skeleton h={220} /> : movements.length === 0 ? (
          <Empty>No OpenSky movements for {iata} in the last 30 days.</Empty>
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={movements} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
              <CartesianGrid stroke={RULE} vertical={false} />
              <XAxis dataKey="label" tick={{ fontSize: 11, fill: MUTED }} />
              <YAxis tick={{ fontSize: 11, fill: MUTED }} allowDecimals={false} />
              <Tooltip />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Bar dataKey="arrivals" name="Arrivals" fill={BLUE} />
              <Bar dataKey="departures" name="Departures" fill="#93c5fd" />
            </BarChart>
          </ResponsiveContainer>
        )}
      </Section>

      <Section
        title="Weather penalty"
        note="Median excess terminal time with and without each condition, all time. Read alongside n: small samples are noisy."
      >
        {penaltyQ.isLoading ? <Skeleton h={140} /> : rowsOf(penaltyQ.data).length === 0 ? (
          <Empty>No arrivals at {iata} with a current METAR yet.</Empty>
        ) : (
          <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
            <thead>
              <tr>
                <th style={th}>Condition</th>
                <th style={{ ...th, textAlign: "right" }}>With</th>
                <th style={{ ...th, textAlign: "right" }}>n</th>
                <th style={{ ...th, textAlign: "right" }}>Without</th>
                <th style={{ ...th, textAlign: "right" }}>n</th>
                <th style={{ ...th, textAlign: "right" }}>Difference</th>
              </tr>
            </thead>
            <tbody>
              {rowsOf(penaltyQ.data).map((r) => (
                <tr key={r.condition}>
                  <td style={td}>{r.condition}</td>
                  <td style={num}>{mins(r.with_condition)}</td>
                  <td style={{ ...num, color: MUTED }}>{N(r.n_with)}</td>
                  <td style={num}>{mins(r.without_condition)}</td>
                  <td style={{ ...num, color: MUTED }}>{N(r.n_without)}</td>
                  <td style={{ ...num, fontWeight: 600 }}>
                    {r.with_condition == null || r.without_condition == null
                      ? "–" : mins(N(r.with_condition) - N(r.without_condition))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section
        title={arrivals.length ? `All arrivals, ${arrivals[0].day} (${iata} local)` : "All arrivals"}
        note="Every arrival OpenSky observed on the latest local day with data, from any origin. Terminal time only where the flight's track was fetched and well covered."
      >
        {arrivalsQ.isLoading ? <Skeleton h={200} /> : arrivals.length === 0 ? (
          <Empty>No OpenSky arrivals at {iata} yet.</Empty>
        ) : (
          <div style={{ maxHeight: 420, overflowY: "auto" }}>
            <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
              <thead style={{ position: "sticky", top: 0, background: "#ffffff" }}>
                <tr>
                  <th style={th}>Arrived</th>
                  <th style={th}>Flight</th>
                  <th style={th}>Airline</th>
                  <th style={th}>From</th>
                  <th style={th}>Category</th>
                  <th style={{ ...th, textAlign: "right" }}>Terminal time</th>
                  <th style={{ ...th, textAlign: "right" }}>Excess</th>
                </tr>
              </thead>
              <tbody>
                {arrivals.map((r, i) => (
                  <tr key={i}>
                    <td style={td}>{r.arrived}</td>
                    <td style={td} title={r.callsign ?? undefined}>{r.flight}</td>
                    <td style={{ ...td, color: MUTED }}>{r.airline_name ?? ""}</td>
                    <td style={td} title={r.departure_icao ?? undefined}>{r.origin ?? "–"}</td>
                    <td style={{ ...td, color: r.flight_category ? CATEGORY_COLORS[r.flight_category] : MUTED }}>
                      {r.flight_category ?? "–"}
                    </td>
                    <td style={num}>{r.terminal_minutes == null ? "–" : `${N(r.terminal_minutes).toFixed(1)} min`}</td>
                    <td style={num}>{mins(r.excess_terminal_minutes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p style={{ fontSize: 12, color: MUTED, margin: "8px 0 0" }}>
          {arrivals.length} arrivals. Hover a flight for its ICAO callsign, an origin for its ICAO code.
        </p>
      </Section>

      <Section title="Slowest arrivals, last 30 days" note="Most excess terminal-area time, with the weather when they landed.">
        {worstQ.isLoading ? <Skeleton h={200} /> : rowsOf(worstQ.data).length === 0 ? (
          <Empty>No benchmarked arrivals at {iata} yet. Each needs at least one earlier arrival in the 30-day baseline.</Empty>
        ) : (
          <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
            <thead>
              <tr>
                <th style={th}>Arrived</th>
                <th style={th}>Flight</th>
                <th style={th}>From</th>
                <th style={{ ...th, textAlign: "right" }}>Terminal time</th>
                <th style={{ ...th, textAlign: "right" }}>Excess</th>
                <th style={th}>Category</th>
                <th style={{ ...th, textAlign: "right" }}>Gust</th>
                <th style={th}>Weather</th>
              </tr>
            </thead>
            <tbody>
              {rowsOf(worstQ.data).map((r, i) => (
                <tr key={i}>
                  <td style={td}>{r.arrived}</td>
                  <td style={td}>{r.flight}</td>
                  <td style={td}>{r.origin ?? "–"}</td>
                  <td style={num}>{N(r.terminal_minutes).toFixed(1)} min</td>
                  <td style={{ ...num, fontWeight: 600 }}>{mins(r.excess_terminal_minutes)}</td>
                  <td style={{ ...td, color: r.flight_category ? CATEGORY_COLORS[r.flight_category] : MUTED }}>
                    {r.flight_category ?? "–"}
                  </td>
                  <td style={num}>{r.wind_gust_kt == null ? "–" : `${N(r.wind_gust_kt)} kt`}</td>
                  <td style={{ ...td, color: MUTED }}>{r.wx_string ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section title="Departures, last 30 days" note="Time to leave the terminal area: first airborne position to crossing 50 NM, against the airport's rolling 30-day median.">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 24 }}>
          {depKpiQ.isLoading || !depKpi ? <Skeleton h={56} /> : (
            <>
              <KPI label="Departures observed, 30 days" value={String(N(depKpi.observed))} />
              <KPI label="With terminal time, 30 days" value={String(N(depKpi.with_time))} />
              <KPI label="Median time to leave 50 NM"
                value={depKpi.median_minutes == null ? "–" : `${N(depKpi.median_minutes).toFixed(1)} min`} />
              <KPI label="Median excess vs baseline" value={mins(depKpi.median_excess)} />
              <KPI label="Departures in IFR / LIFR" value={pct(depKpi.ifr_share)} />
            </>
          )}
        </div>
      </Section>

      <Section
        title={departures.length ? `All departures, ${departures[0].day} (${iata} local)` : "All departures"}
        note="Every departure OpenSky observed on the latest local day with data, to any destination. Terminal time only where the flight's track was fetched (tracks are fetched for flights landing at one of the five airports)."
      >
        {departuresQ.isLoading ? <Skeleton h={200} /> : departuresQ.isError ? (
          <Empty>Could not load departures: {String(departuresQ.error?.message ?? departuresQ.error)}</Empty>
        ) : departures.length === 0 ? (
          <Empty>No OpenSky departures from {iata} yet.</Empty>
        ) : (
          <div style={{ maxHeight: 420, overflowY: "auto" }}>
            <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
              <thead style={{ position: "sticky", top: 0, background: "#ffffff" }}>
                <tr>
                  <th style={th}>Departed</th>
                  <th style={th}>Flight</th>
                  <th style={th}>Airline</th>
                  <th style={th}>To</th>
                  <th style={th}>Category</th>
                  <th style={{ ...th, textAlign: "right" }}>Time to 50 NM</th>
                  <th style={{ ...th, textAlign: "right" }}>Excess</th>
                </tr>
              </thead>
              <tbody>
                {departures.map((r, i) => (
                  <tr key={i}>
                    <td style={td}>{r.departed}</td>
                    <td style={td} title={r.callsign ?? undefined}>{r.flight}</td>
                    <td style={{ ...td, color: MUTED }}>{r.airline_name ?? ""}</td>
                    <td style={td} title={r.arrival_icao ?? undefined}>{r.destination ?? "–"}</td>
                    <td style={{ ...td, color: r.flight_category ? CATEGORY_COLORS[r.flight_category] : MUTED }}>
                      {r.flight_category ?? "–"}
                    </td>
                    <td style={num}>{r.departure_terminal_minutes == null ? "–" : `${N(r.departure_terminal_minutes).toFixed(1)} min`}</td>
                    <td style={num}>{mins(r.excess_departure_minutes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p style={{ fontSize: 12, color: MUTED, margin: "8px 0 0" }}>
          {departures.length} departures. Hover a flight for its ICAO callsign, a destination for its ICAO code.
        </p>
      </Section>
    </div>
  );
}
