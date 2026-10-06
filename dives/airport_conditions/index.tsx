// MotherDuck Dive: per-airport weather, traffic, arrival and departure impact.
// Reads aviation.marts / aviation.reference only. Airports and flights are shown with
// IATA codes (SYD, QF627); the marts keep the ICAO forms too, and filters use ICAO. A
// flight with no IATA flight number shows its ATC callsign muted (FlightCode).
// Flight times are shown in the airport's local time (reference.airports.timezone).
// Published by scripts/deploy_motherduck.py; preview locally with `motherduck dive watch`.
// The same file is the Vercel site: web/ bundles it and runs its SQL on DuckDB-WASM.
// Live aircraft come from the Vercel API (/api/live), which proxies OpenSky.
import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
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

// ---- Theme: colours are CSS variables on the root element, so inline styles, SVG and
// Recharts all switch together. "auto" follows the system; a manual choice is remembered.
const LIGHT = {
  bg: "#ffffff", surface: "#ffffff", ink: "#1a1a1a", muted: "#6a6a6a", rule: "#e5e5e5",
  "row-active": "#f3f4f6", code: "#f6f7f9", panel: "rgba(255,255,255,0.94)", "panel-faint": "rgba(255,255,255,0.8)",
  "map-bg": "#eef1f4", halo: "#ffffff",
  blue: "#2563eb", "blue-soft": "#93c5fd", orange: "#ea580c",
  vfr: "#16a34a", mvfr: "#2563eb", ifr: "#dc2626", lifr: "#c026d3",
  "rag-green": "#16a34a", "rag-amber": "#d97706", "rag-red": "#dc2626", "rag-unknown": "#8d939c",
  ground: "#c4c8cf", other: "#b3b8bf",
};
const DARK: typeof LIGHT = {
  bg: "#121417", surface: "#1b1e23", ink: "#e6e7e9", muted: "#9aa0a8", rule: "#2e333a",
  "row-active": "#23272e", code: "#1b1e23", panel: "rgba(27,30,35,0.94)", "panel-faint": "rgba(27,30,35,0.8)",
  "map-bg": "#1f2226", halo: "#121417",
  blue: "#60a5fa", "blue-soft": "#1e4fa8", orange: "#fb923c",
  vfr: "#22c55e", mvfr: "#60a5fa", ifr: "#f87171", lifr: "#e879f9",
  "rag-green": "#22c55e", "rag-amber": "#fbbf24", "rag-red": "#f87171", "rag-unknown": "#8d939c",
  ground: "#5b616a", other: "#6b7179",
};
const cssVars = (p: typeof LIGHT) => Object.entries(p).map(([k, v]) => `--ac-${k}:${v};`).join("");
const THEME_CSS = `.airport-conditions{${cssVars(LIGHT)}color-scheme:light}`
  + `.airport-conditions[data-theme="dark"]{${cssVars(DARK)}color-scheme:dark}`;
const c = (k: keyof typeof LIGHT) => `var(--ac-${k})`;

type ThemeMode = "auto" | "light" | "dark";
const THEME_KEY = "airport-conditions-theme";
const darkQuery = () =>
  typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;

function useTheme() {
  // Storage can be blocked (private windows, sandboxed hosts); then the choice lasts the visit.
  const [mode, setModeState] = useState<ThemeMode>(() => {
    try {
      const saved = localStorage.getItem(THEME_KEY);
      if (saved === "light" || saved === "dark") return saved;
    } catch { /* fall through */ }
    return "auto";
  });
  const [systemDark, setSystemDark] = useState(() => darkQuery()?.matches ?? false);
  useEffect(() => {
    const q = darkQuery();
    if (!q) return;
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches);
    q.addEventListener("change", onChange);
    return () => q.removeEventListener("change", onChange);
  }, []);
  const setMode = (next: ThemeMode) => {
    try {
      if (next === "auto") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, next);
    } catch { /* keep it for this visit only */ }
    setModeState(next);
  };
  return { mode, setMode, dark: mode === "dark" || (mode === "auto" && systemDark) };
}

function ThemeSwitch({ mode, onChange }: { mode: ThemeMode; onChange: (m: ThemeMode) => void }) {
  return (
    <div role="group" aria-label="Colour theme"
      style={{ display: "inline-flex", border: `1px solid ${RULE}`, borderRadius: 6, overflow: "hidden", flex: "none" }}>
      {(["auto", "light", "dark"] as const).map((m) => (
        <button key={m} onClick={() => onChange(m)} aria-pressed={mode === m}
          style={{
            border: "none", padding: "3px 10px", fontSize: 12, cursor: "pointer", fontFamily: "inherit",
            background: mode === m ? c("row-active") : c("surface"), color: mode === m ? INK : MUTED,
            fontWeight: mode === m ? 600 : 400,
          }}>
          {humanize(m)}
        </button>
      ))}
    </div>
  );
}

const INK = c("ink");
const MUTED = c("muted");
const RULE = c("rule");
const BLUE = c("blue");
const ORANGE = c("orange");
// Standard aviation flight-category colours.
const CATEGORY_COLORS: Record<string, string> = {
  VFR: c("vfr"), MVFR: c("mvfr"), IFR: c("ifr"), LIFR: c("lifr"),
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

// A flight as shown: its IATA flight number, else the ATC callsign (or transponder address)
// muted, so a callsign never passes for a flight number.
function FlightCode({ iata, callsign, icao24 }: { iata?: string | null; callsign?: string | null; icao24?: string | null }) {
  if (iata) return <span title={callsign ?? undefined}>{iata}</span>;
  if (callsign) return <span style={{ color: MUTED }} title={`${callsign}: ATC callsign, no IATA flight number for it`}>{callsign}</span>;
  return <span style={{ color: MUTED }} title={`${icao24}: transponder address, no callsign`}>{icao24 ?? "–"}</span>;
}

const mono: CSSProperties = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12, background: c("code"),
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
  const [state, setState] = useState<{ aircraft: Live[]; source: string; at: Date | null; error: string | null }>(
    { aircraft: [], source: "", at: null, error: null });
  useEffect(() => {
    if (!icao) return;
    let stale = false;
    const load = () => fetch(`${API_BASE}/api/live/${icao}`)
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body?.detail ?? `HTTP ${r.status}`);
        return body;
      })
      .then((b) => !stale && setState({ aircraft: b.aircraft ?? [], source: b.source ?? "", at: new Date(), error: null }))
      .catch((e) => !stale && setState((s) => ({ ...s, error: String(e?.message ?? e) })));
    setState({ aircraft: [], source: "", at: null, error: null });
    load();
    const id = setInterval(load, LIVE_REFRESH_MS);
    return () => { stale = true; clearInterval(id); };
  }, [icao]);
  return state;
}

// The airport's own flight board, where it publishes one (/api/schedule: Hong Kong for
// now), refreshed every 3 minutes. Elsewhere `source` stays null and the boards predict
// from each callsign's usual time.
type Sched = {
  dir: Dir; flight: string; codeshares: string[]; callsign: string | null; other: string | null;
  scheduled_at: Date; estimated_at: Date | null; actual_at: Date | null;
  state: string; status: string | null; stand: string | null; gate: string | null; cargo: boolean;
};
// As the API sends it: times are ISO strings.
type SchedJson = Omit<Sched, "scheduled_at" | "estimated_at" | "actual_at">
  & { scheduled_at: string; estimated_at: string | null; actual_at: string | null };
const SCHEDULE_REFRESH_MS = 180_000; // matches the API's edge cache

function useSchedule(icao: string) {
  const [state, setState] = useState<{ flights: Sched[]; source: string | null; error: string | null }>(
    { flights: [], source: null, error: null });
  useEffect(() => {
    if (!icao) return;
    let stale = false;
    const date = (v: string | null) => (v ? new Date(v) : null);
    const load = () => fetch(`${API_BASE}/api/schedule/${icao}`)
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body?.detail ?? `HTTP ${r.status}`);
        return body;
      })
      .then((b) => !stale && setState({
        flights: b.available ? (b.flights ?? []).map((f: SchedJson): Sched => ({
          ...f, scheduled_at: new Date(f.scheduled_at), estimated_at: date(f.estimated_at), actual_at: date(f.actual_at),
        })) : [],
        source: b.available ? b.source : null, error: null,
      }))
      // Keep the last board: a failed refresh should not blank it.
      .catch((e) => !stale && setState((s) => ({ ...s, error: String(e?.message ?? e) })));
    setState({ flights: [], source: null, error: null });
    load();
    const id = setInterval(load, SCHEDULE_REFRESH_MS);
    return () => { stale = true; clearInterval(id); };
  }, [icao]);
  return state;
}

// ADS-B callsigns keep or drop leading zeros (CPA0710, CPA710); the schedule's drop them.
const callsignKey = (c: string) => c.replace(/^([A-Z]{3})0*(\d{1,4})$/, "$1$2");
const schedTime = (s: Sched) => s.actual_at ?? s.estimated_at ?? s.scheduled_at;

// The scheduled flight a live callsign is flying in one direction: the one whose latest
// time is nearest `near`, as a flight number repeats from day to day. Further than 4 hours
// out it is more likely another day's flight, or a callsign reused, than this one.
const SCHED_MATCH_MS = 4 * 3_600_000;
function schedFor(index: Map<string, Sched[]>, callsign: string | null, dir: Dir, near: Date): Sched | null {
  if (!callsign) return null;
  let best: Sched | null = null;
  const gap = (x: Sched) => Math.abs(schedTime(x).getTime() - near.getTime());
  for (const s of index.get(callsignKey(callsign)) ?? []) {
    if (s.dir !== dir || s.state === "cancelled" || gap(s) > SCHED_MATCH_MS) continue;
    if (!best || gap(s) < gap(best)) best = s;
  }
  return best;
}

type TrackLine = { key: string; role: string; label: string; points: [number, number][] };

// A live aircraft placed relative to the selected airport.
type Placed = Live & {
  dir: "inbound" | "outbound" | "ground" | "other";
  other: string | null;      // origin (inbound) or destination (outbound), IATA where known
  flight_iata: string | null; // IATA flight number where the callsign maps to one
  label: string;             // that, else the callsign, else the transponder address
  dist_nm: number;
  eta_min: number | null;    // inbound and airborne only: distance / ground speed
  usual: string | null;      // the flight's usual local arrival / departure time, "HH:MM"
  delay_min: number | null;  // estimated minutes late against that usual time
  rag: Rag;
  event_at: Date | null;     // estimated arrival (inbound) or take-off (outbound)
  sched: Sched | null;       // the airport's scheduled flight, where it publishes a schedule
};

type Dir = "inbound" | "outbound";
// Per callsign and direction over the last 30 days: usual origin / destination, usual
// local time (minutes after midnight) and days seen out of the last 14.
type History = Map<string, Partial<Record<Dir, { other: string | null; usual: number; days: number }>>>;

// Delay status. OpenSky has no schedules, so "late" means later than the flight's usual
// time at this airport over the last 30 days. Bands follow the 15-minute on-time convention.
type Rag = "green" | "amber" | "red" | "unknown";
const RAG_COLORS: Record<Rag, string> = {
  green: c("rag-green"), amber: c("rag-amber"), red: c("rag-red"), unknown: c("rag-unknown"),
};
const ragOf = (delay: number | null): Rag =>
  delay == null ? "unknown" : delay < 15 ? "green" : delay < 45 ? "amber" : "red";
const ragText = (a: Placed) =>
  a.delay_min == null ? "No usual time" : a.delay_min < -15 ? `Early ${Math.round(-a.delay_min)} min`
    : a.delay_min < 15 ? "On time" : `Late ${Math.round(a.delay_min)} min`;
const OTHER_COLORS = { ground: c("ground"), other: c("other") };
const markerColor = (a: Placed) =>
  a.dir === "inbound" || a.dir === "outbound" ? RAG_COLORS[a.rag] : OTHER_COLORS[a.dir];

// Minutes after local midnight in `timeZone`, and the signed difference of two such times
// wrapped to [-720, 720) so 23:50 vs 00:10 is -20, not 1420.
function minuteOfDay(at: Date, timeZone: string) {
  const [h, m] = new Intl.DateTimeFormat("en-GB", { timeZone, hour: "2-digit", minute: "2-digit", hour12: false })
    .format(at).split(":").map(Number);
  return (h % 24) * 60 + m;
}
const wrapMinutes = (d: number) => ((((d + 720) % 1440) + 1440) % 1440) - 720;
const hhmm = (minutes: number) => {
  const m = Math.round(minutes) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
};

function distKm(la1: number, lo1: number, la2: number, lo2: number) {
  const r = Math.PI / 180;
  const h = Math.sin(((la2 - la1) * r) / 2) ** 2
    + Math.cos(la1 * r) * Math.cos(la2 * r) * Math.sin(((lo2 - lo1) * r) / 2) ** 2;
  return 2 * 6371.0088 * Math.asin(Math.sqrt(h));
}

// Initial great-circle bearing from point 1 to point 2, degrees.
function bearingDeg(la1: number, lo1: number, la2: number, lo2: number) {
  const r = Math.PI / 180;
  const y = Math.sin((lo2 - lo1) * r) * Math.cos(la2 * r);
  const x = Math.cos(la1 * r) * Math.sin(la2 * r) - Math.sin(la1 * r) * Math.cos(la2 * r) * Math.cos((lo2 - lo1) * r);
  return ((Math.atan2(y, x) / r) + 360) % 360;
}

// Compass with the wind blowing across it: tail on the "from" bearing, head downwind,
// matching the arrow on the map. VRB and calm have no direction to draw.
function WindCompass({ wx, size = 64 }: { wx: Record<string, any>; size?: number }) {
  const r = size / 2;
  const calm = wx.wind_speed_kt == null || N(wx.wind_speed_kt) === 0;
  const from = !calm && !wx.wind_variable && wx.wind_dir_deg != null ? (N(wx.wind_dir_deg) * Math.PI) / 180 : null;
  const p = (a: number, d: number) => [r + d * Math.sin(a), r - d * Math.cos(a)];
  const [x1, y1] = from == null ? [0, 0] : p(from, r - 10);
  const [x2, y2] = from == null ? [0, 0] : p(from + Math.PI, r - 12);
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ flex: "none" }}>
      <defs>
        <marker id="compass-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="4" markerHeight="4" orient="auto">
          <path d="M0,0 L10,5 L0,10 z" fill={INK} />
        </marker>
      </defs>
      <circle cx={r} cy={r} r={r - 6} fill={c("surface")} stroke={RULE} />
      {["N", "E", "S", "W"].map((c, i) => {
        const [x, y] = p((i * Math.PI) / 2, r - 3);
        return <text key={c} x={x} y={y} dy={3} textAnchor="middle" fontSize={7} fill={MUTED}>{c}</text>;
      })}
      {from != null ? (
        <line x1={x1} y1={y1} x2={x2} y2={y2} stroke={INK} strokeWidth={2.5} markerEnd="url(#compass-head)" />
      ) : (
        <text x={r} y={r} dy={3} textAnchor="middle" fontSize={9} fontWeight={600} fill={MUTED}>
          {calm ? "Calm" : "VRB"}
        </text>
      )}
    </svg>
  );
}

const overlayPanel: CSSProperties = {
  background: c("panel"), border: `1px solid ${RULE}`, borderRadius: 8, padding: "8px 10px",
  fontSize: 12, color: INK, boxShadow: "0 1px 3px rgba(0,0,0,0.08)", pointerEvents: "auto",
};

// The aircraft clicked on the map, shown under the weather panel.
function FlightPanel({ a, iata, tz, onClose }: { a: Placed; iata: string; tz: string; onClose: () => void }) {
  const hm = (d: Date) => clockParts(d, tz).time.slice(0, 5);
  const tracked = a.dir === "inbound" || a.dir === "outbound";
  const row = (label: string, value: string) => (
    <div style={{ display: "contents" }}>
      <span style={{ color: MUTED }}>{label}</span>
      <span style={{ fontWeight: 600 }}>{value}</span>
    </div>
  );
  return (
    <div style={{ ...overlayPanel, marginTop: "auto", minHeight: 0, overflowY: "auto", textAlign: "left" }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 6 }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 600 }}>{a.label}</div>
          {a.callsign && a.callsign !== a.label && <div style={{ color: MUTED }}>{a.callsign}</div>}
        </div>
        <button onClick={onClose} aria-label="Hide flight"
          style={{ marginLeft: "auto", border: "none", background: "none", cursor: "pointer", color: MUTED, fontSize: 14, lineHeight: 1 }}>
          ×
        </button>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6, margin: "4px 0 6px" }}>
        <span style={{ width: 8, height: 8, borderRadius: 4, background: markerColor(a), flex: "none" }} />
        <span style={{ fontWeight: 600 }}>
          {a.dir === "inbound" ? `${a.other ?? "?"} → ${iata}` : a.dir === "outbound" ? `${iata} → ${a.other ?? "?"}`
            : a.dir === "ground" ? `On the ground at ${iata}` : "Other traffic"}
        </span>
        {tracked && <span style={{ color: MUTED }}>· {ragText(a)}</span>}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "2px 10px" }}>
        {row("Distance", `${Math.round(a.dist_nm)} NM`)}
        {row("Altitude", a.on_ground ? "Ground" : `${a.alt_ft.toLocaleString()} ft`)}
        {row("Speed", a.speed_kt == null ? "–" : `${a.speed_kt} kt`)}
        {a.vrate_fpm ? row("Vertical", `${a.vrate_fpm > 0 ? "+" : ""}${a.vrate_fpm} ft/min`) : null}
        {a.track_deg != null && row("Heading", `${String(Math.round(N(a.track_deg))).padStart(3, "0")}°`)}
        {a.usual && row(a.dir === "inbound" ? "Usual arrival" : "Usual departure", a.usual)}
        {a.event_at && !a.on_ground && (a.dir === "inbound"
          ? row("ETA", `${hm(a.event_at)} (${Math.round(a.eta_min ?? 0)} min)`)
          : row("Took off", `~${hm(a.event_at)}`))}
      </div>
    </div>
  );
}

// The selected airport's latest METAR, laid over the map.
function WeatherPanel({ wx, onClose }: { wx: Record<string, any>; onClose: () => void }) {
  const cat = wx.flight_category as string | null;
  const row = (label: string, value: string) => (
    <div style={{ display: "contents" }}>
      <span style={{ color: MUTED }}>{label}</span>
      <span style={{ fontWeight: 600, textAlign: "right" }}>{value}</span>
    </div>
  );
  return (
    <div style={overlayPanel}>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{ fontWeight: 600 }}>{wx.iata} weather</span>
        {cat && (
          <span style={{ background: CATEGORY_COLORS[cat] ?? MUTED, color: c("bg"), borderRadius: 4, padding: "0 5px", fontSize: 11, fontWeight: 600 }}>
            {cat}
          </span>
        )}
        <button onClick={onClose} aria-label="Hide weather"
          style={{ marginLeft: "auto", border: "none", background: "none", cursor: "pointer", color: MUTED, fontSize: 14, lineHeight: 1 }}>
          ×
        </button>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "6px 0" }}>
        <WindCompass wx={wx} />
        <div>
          <div style={{ fontSize: 16, fontWeight: 600 }}>{windText(wx)}</div>
          <div style={{ color: MUTED }}>
            {wx.wind_variable ? "Variable direction"
              : wx.wind_dir_deg != null && N(wx.wind_speed_kt) > 0 ? `From ${String(N(wx.wind_dir_deg)).padStart(3, "0")}°` : "Wind"}
            {wx.wind_gust_kt != null ? `, gusting ${N(wx.wind_gust_kt)} kt` : ""}
          </div>
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "2px 10px" }}>
        {row("Visibility", visText(wx))}
        {row("Ceiling", wx.ceiling_ft == null ? "None" : `${N(wx.ceiling_ft).toLocaleString()} ft`)}
        {row("Temp / dew", wx.temp_c == null ? "–" : `${N(wx.temp_c)}° / ${wx.dewpoint_c == null ? "–" : N(wx.dewpoint_c)}°C`)}
        {row("QNH", wx.altimeter_hpa == null ? "–" : `${Math.round(N(wx.altimeter_hpa))} hPa`)}
        {row("Weather", wx.wx_string ?? "Nil")}
      </div>
      {wx.metar_at && (
        <div style={{ color: MUTED, fontSize: 11, marginTop: 6 }}>METAR {wx.metar_at}, {N(wx.metar_age_min)} min ago</div>
      )}
    </div>
  );
}

function AirspaceMap({ lat, lon, wx, tracks, live, dark, iata, tz }: {
  lat: number; lon: number; wx: Record<string, any> | undefined; tracks: TrackLine[]; live: Placed[]; dark: boolean;
  iata: string; tz: string;
}) {
  const [zoom, setZoom] = useState(8);
  const [showWeather, setShowWeather] = useState(true);
  // By transponder address, so the panel follows the aircraft across live refreshes.
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => setSelected(null), [lat, lon]);
  const sel = live.find((a) => a.icao24 === selected);
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
        key: `${dark}/${zoom}/${tx}/${ty}`,
        // Esri's grey canvases need no API key (CARTO's basemaps now do).
        href: `https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${dark ? "Dark" : "Light"}_Gray_Base/MapServer/tile/${zoom}/${ty}/${wxTile}`,
        x: tx * TILE - x0, y: ty * TILE - y0,
      });
    }
  }
  const pxPerKm = (TILE * n) / (40075.017 * Math.cos((lat * Math.PI) / 180));
  const catColor = wx?.flight_category ? CATEGORY_COLORS[wx.flight_category] : MUTED;
  const btn: CSSProperties = {
    width: 28, height: 28, border: `1px solid ${RULE}`, background: c("surface"), borderRadius: 6,
    fontSize: 16, lineHeight: "24px", cursor: "pointer", color: INK,
  };

  return (
    <div style={{ position: "relative", border: `1px solid ${RULE}`, borderRadius: 8, overflow: "hidden" }}>
      <svg viewBox={`0 0 ${MAP_W} ${MAP_H}`} onClick={() => setSelected(null)}
        style={{ width: "100%", height: "auto", display: "block", background: c("map-bg") }}>
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
          const label = a.label;
          const tracked = a.dir === "inbound" || a.dir === "outbound";
          return (
            <g key={a.icao24} transform={`translate(${x.toFixed(1)},${y.toFixed(1)})`} style={{ cursor: "pointer" }}
              onClick={(e) => { e.stopPropagation(); setSelected(a.icao24 === selected ? null : a.icao24); }}>
              {/* A wider hit area than the glyph, and a ring on the selected aircraft. */}
              <circle r={11} fill="transparent" stroke={a.icao24 === selected ? INK : "none"} strokeWidth={1.5} />
              <path d="M0,-8 L5.5,6 L0,3 L-5.5,6 Z" transform={`rotate(${N(a.track_deg)}) scale(${tracked ? 1.15 : 0.9})`}
                fill={markerColor(a)} stroke={c("halo")} strokeWidth={0.8} />
              {(tracked || zoom >= 9) && zoom >= 8 && (
                <text x={9} y={4} fontSize={10} fill={tracked ? markerColor(a) : MUTED}>{label}</text>
              )}
              <title>
                {`${label}${a.callsign && label !== a.callsign ? ` (${a.callsign})` : ""}`
                  + (a.dir === "inbound" ? ` from ${a.other ?? "?"}` : a.dir === "outbound" ? ` to ${a.other ?? "?"}` : "")
                  + (a.dir === "inbound" || a.dir === "outbound" ? ` · ${ragText(a)}${a.usual ? ` (usually ${a.usual})` : ""}` : "")
                  + `\n${Math.round(a.dist_nm)} NM out, `
                  + (a.on_ground ? "On ground" : `${a.alt_ft.toLocaleString()} ft`)
                  + (a.speed_kt != null ? `, ${a.speed_kt} kt` : "")
                  + (a.vrate_fpm ? `, ${a.vrate_fpm > 0 ? "+" : ""}${a.vrate_fpm} ft/min` : "")}
              </title>
            </g>
          );
        })}
        <circle cx={MAP_W / 2} cy={MAP_H / 2} r={9} fill={catColor} stroke={c("halo")} strokeWidth={2.5}>
          <title>{`${wx?.flight_category ?? "No current category"} · ${wx ? windText(wx) : ""}`}</title>
        </circle>
      </svg>
      <div style={{ position: "absolute", top: 10, left: 10, display: "flex", flexDirection: "column", gap: 4 }}>
        <button style={btn} onClick={() => setZoom((z) => Math.min(11, z + 1))} aria-label="Zoom in">+</button>
        <button style={btn} onClick={() => setZoom((z) => Math.max(6, z - 1))} aria-label="Zoom out">−</button>
      </div>
      {/* Right-hand column: weather at the top, the clicked aircraft anchored to the bottom. */}
      <div style={{
        position: "absolute", top: 10, right: 10, bottom: 24, width: 230,
        display: "flex", flexDirection: "column", alignItems: "stretch", gap: 8, pointerEvents: "none",
      }}>
        {wx?.metar_raw != null && (showWeather
          ? <WeatherPanel wx={wx} onClose={() => setShowWeather(false)} />
          : (
            <button onClick={() => setShowWeather(true)}
              style={{ ...btn, alignSelf: "flex-end", width: "auto", padding: "0 10px", fontSize: 12, fontWeight: 600, pointerEvents: "auto" }}>
              {wx.iata} weather
            </button>
          ))}
        {sel && <FlightPanel a={sel} iata={iata} tz={tz} onClose={() => setSelected(null)} />}
      </div>
      <div style={{ position: "absolute", right: 6, bottom: 4, fontSize: 10, color: MUTED, background: c("panel-faint"), padding: "0 4px" }}>
        Esri, HERE, Garmin, © OpenStreetMap contributors
      </div>
    </div>
  );
}

// ---- NOTAMs: full text of every NOTAM in force, filterable by Q-code category ----------
const NOTAM_SOURCES: Record<string, string> = {
  hk_cad: "Hong Kong CAD", faa_search: "FAA NOTAM Search", faa: "FAA NOTAM API", rapidapi: "SkyLink on RapidAPI",
};
// "movement_area" -> "Movement area"
const humanize = (v: string) => (v.charAt(0).toUpperCase() + v.slice(1)).replace(/_/g, " ");

function NotamList({ rows }: { rows: Record<string, any>[] }) {
  const [category, setCategory] = useState("all");
  const catOf = (r: Record<string, any>) => (r.category as string | null) ?? "uncategorised";
  const counts = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of rows) m.set(catOf(r), (m.get(catOf(r)) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [rows]);
  const shown = category === "all" ? rows : rows.filter((r) => catOf(r) === category);
  return (
    <>
      <select
        value={category}
        onChange={(e) => setCategory(e.target.value)}
        style={{ fontSize: 13, padding: "3px 6px", border: `1px solid ${RULE}`, borderRadius: 6, marginBottom: 8, background: c("surface"), color: INK }}
      >
        <option value="all">All categories ({rows.length})</option>
        {counts.map(([c, n]) => <option key={c} value={c}>{humanize(c)} ({n})</option>)}
      </select>
      <div style={{ maxHeight: 560, overflowY: "auto", borderTop: `1px solid ${RULE}` }}>
        {shown.map((r) => (
          <div key={r.notam_key} style={{ padding: "10px 0", borderBottom: `1px solid ${RULE}` }}>
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: "2px 12px", fontSize: 13 }}>
              <span style={{ fontWeight: 600 }}>{r.number}</span>
              {r.category && (
                <span style={{ color: MUTED }}>
                  {humanize(r.category)}{r.condition ? ` · ${humanize(r.condition)}` : ""}
                </span>
              )}
              <span style={{ color: MUTED }}>
                {r.from_txt ?? "?"} to {r.is_permanent ? "PERM" : r.to_txt ?? "?"}{r.is_estimated ? " (estimated)" : ""}
              </span>
              {r.has_schedule && <span style={{ color: ORANGE }}>Active only: {r.schedule}</span>}
            </div>
            <pre style={mono}>{r.raw_text}</pre>
          </div>
        ))}
      </div>
    </>
  );
}

const th: CSSProperties = {
  textAlign: "left", fontWeight: 500, color: MUTED, padding: "6px 12px 6px 0", borderBottom: `1px solid ${RULE}`,
};
const td: CSSProperties = { padding: "6px 12px 6px 0", borderBottom: `1px solid ${RULE}` };
const num: CSSProperties = { ...td, textAlign: "right", fontVariantNumeric: "tabular-nums" };
const tooltipProps = {
  contentStyle: { background: c("surface"), border: `1px solid ${RULE}`, borderRadius: 6, fontSize: 12 },
  labelStyle: { color: INK },
  cursor: { fill: RULE, stroke: RULE },
};

// ---- Arrivals / departures boards: past few hours and coming up ------------------------
// There is no live schedule, so the boards are assembled from what the live feed showed
// earlier this visit (landed, or gone out of range) and each regular flight's usual time.
// Flights on the feed now are in the En route table instead.
const PAST_HOURS = 3;
const NEXT_HOURS = 6;
const PAST_SHOWN = 5; // latest past flights shown before "Show all"
const REGULAR_DAYS = 7; // seen on at least this many of the last 14 days
const TERMINAL_NM = TERMINAL_KM / 1.852;

// A recognised flight on the live feed, with its direction settled (on-ground aircraft
// are assigned one from their usual times).
type BoardLive = {
  callsign: string; dir: Dir; label: string; flight_iata: string | null; other: string | null; usual: string | null;
  on_ground: boolean; dist_nm: number; event_at: Date | null; rag: Rag; status_note: string | null;
  aircraft: Placed; sched: Sched | null;
};
// En route is flights still to arrive or depart: an arrival already on the ground here is
// on the Arrivals board instead, while a departure on the ground is still to leave.
const isEnRoute = (r: BoardLive) => !(r.dir === "inbound" && r.on_ground);
// What the feed showed of a flight this visit.
type Remembered = BoardLive & { last_at: Date; gone_at: Date | null; landed_at: Date | null };

function useFeedMemory(icao: string, rows: BoardLive[], at: Date | null) {
  const [memory, setMemory] = useState<Map<string, Remembered>>(() => new Map());
  useEffect(() => setMemory(new Map()), [icao]);
  useEffect(() => {
    if (!at) return;
    setMemory((prev) => {
      const next = new Map(prev);
      for (const r of rows) {
        const old = next.get(r.callsign);
        // Airborne inbound at the last fix, on the ground here now: it landed in between.
        const landed_at = r.dir === "inbound" && r.on_ground && old && !old.on_ground ? at : old?.landed_at ?? null;
        next.set(r.callsign, { ...r, last_at: at, gone_at: null, landed_at });
      }
      // Not in this fix: it left the feed (landed and shut down, or out of range).
      for (const [k, m] of next) if (m.last_at < at && !m.gone_at) next.set(k, { ...m, gone_at: at });
      return next;
    });
  }, [at, rows]);
  return memory;
}

type BoardRow = {
  key: string; phase: "past" | "next"; sort: number; time: string; flight_iata: string | null; callsign: string;
  other: string | null; status: string; usual: string | null;
};

function buildBoard(dir: Dir, rows: BoardLive[], memory: Map<string, Remembered>, history: History,
  now: Date, tz: string, flightIata: (c: string) => string | null): BoardRow[] {
  const at = (d: Date) => clockParts(d, tz).time.slice(0, 5);
  const out: BoardRow[] = [];
  // Flights on the feed now belong to En route, not the board, except arrivals on the ground.
  const placed = new Set(rows.filter(isEnRoute).map((r) => r.callsign));
  const add = (r: Omit<BoardRow, "key">) => { placed.add(r.callsign); out.push({ ...r, key: `${r.phase}|${r.callsign}` }); };
  const arriving = dir === "inbound";

  // Landed and still on the feed: taxiing in, or parked with the transponder on.
  if (arriving) for (const r of rows) {
    if (isEnRoute(r)) continue;
    const landed = memory.get(r.callsign)?.landed_at ?? null;
    add({
      phase: "past", flight_iata: r.flight_iata, callsign: r.callsign, other: r.other, usual: r.usual,
      sort: (landed ?? now).getTime(), time: landed ? `~${at(landed)}` : "–", status: "Landed, on the ground",
    });
  }

  // Past: seen this visit, then gone. Inbound flights count as landed only if they were on
  // the ground or inside the terminal area when they went; further out it is a coverage gap.
  const cutoff = now.getTime() - PAST_HOURS * 3_600_000;
  for (const m of memory.values()) {
    if (m.dir !== dir || !m.gone_at || placed.has(m.callsign)) continue;
    if (arriving && !m.on_ground && m.dist_nm > TERMINAL_NM) continue;
    if (!arriving && m.on_ground) continue; // switched off at the stand
    const when = arriving ? (m.landed_at ?? m.event_at ?? m.gone_at) : (m.event_at ?? m.gone_at);
    if (when.getTime() < cutoff) continue;
    add({
      phase: "past", flight_iata: m.flight_iata, callsign: m.callsign, other: m.other, usual: m.usual,
      sort: when.getTime(), time: `~${at(when)}`,
      status: arriving ? "Landed" : `Departed, ${m.dist_nm > 400 ? "out of 500 NM" : "off the feed"}`,
    });
  }

  // Not seen this visit: regular flights by their usual time.
  const nowMin = minuteOfDay(now, tz);
  for (const [callsign, h] of history) {
    const seen = h[dir];
    if (!seen || seen.days < REGULAR_DAYS || placed.has(callsign)) continue;
    const delta = wrapMinutes(seen.usual - nowMin);
    if (delta < -PAST_HOURS * 60 || delta > NEXT_HOURS * 60) continue;
    const when = new Date(now.getTime() + delta * 60_000);
    add({
      phase: delta < 0 ? "past" : "next", flight_iata: flightIata(callsign), callsign,
      other: seen.other, usual: hhmm(seen.usual), sort: when.getTime(), time: at(when),
      status: delta < 0 ? `Presumed ${arriving ? "landed" : "departed"}, not seen live`
        : arriving ? "Expected, not yet within 500 NM" : "Expected",
    });
  }
  return out.sort((a, b) => a.sort - b.sort);
}

// The board from the airport's own schedule: its flights from 3 hours ago to 6 hours ahead
// by their latest time (actual, else estimated, else scheduled), minus those in En route.
// Past is flights with an actual time, landed on the feed, or cancelled.
function scheduleBoard(dir: Dir, flights: Sched[], rows: BoardLive[], memory: Map<string, Remembered>,
  now: Date, tz: string): BoardRow[] {
  const at = (d: Date) => clockParts(d, tz).time.slice(0, 5);
  // Another day's flight the airport still lists (delayed) shows its date: "19:45 3 Oct".
  const today = clockParts(now, tz).date;
  const scheduled = (d: Date) => {
    const p = clockParts(d, tz);
    return p.date === today ? at(d) : `${at(d)} ${p.date.split(" ").slice(1).join(" ")}`;
  };
  const enRoute = new Set(rows.filter((r) => isEnRoute(r) && r.dir === dir).map((r) => callsignKey(r.callsign)));
  const landed = new Map(rows.filter((r) => !isEnRoute(r)).map((r) => [callsignKey(r.callsign), r]));
  const from = now.getTime() - PAST_HOURS * 3_600_000, to = now.getTime() + NEXT_HOURS * 3_600_000;
  const out: BoardRow[] = [];
  for (const s of flights) {
    const when = schedTime(s);
    if (s.dir !== dir || when.getTime() < from || when.getTime() > to) continue;
    if (s.callsign && enRoute.has(s.callsign)) continue;
    const onGround = dir === "inbound" && s.callsign ? landed.get(s.callsign) : undefined;
    const late = (t: Date) => {
      const m = Math.round((t.getTime() - s.scheduled_at.getTime()) / 60_000);
      return m >= 15 ? `, ${m} min late` : m <= -15 ? `, ${-m} min early` : "";
    };
    const landedAt = onGround ? memory.get(onGround.callsign)?.landed_at : null;
    const status = onGround && !s.actual_at ? `Landed${landedAt ? ` ~${at(landedAt)}` : ""}, on the ground`
      : s.actual_at ? `${{ at_gate: "At gate", landed: "Landed", departed: "Departed" }[s.state] ?? "Actual"} ${at(s.actual_at)}${late(s.actual_at)}`
      : s.estimated_at ? `Estimated ${at(s.estimated_at)}${late(s.estimated_at)}`
      : ({ scheduled: "Scheduled", cancelled: "Cancelled", delayed: "Delayed", boarding_soon: "Boarding soon",
          boarding: "Boarding", final_call: "Final call", gate_closed: "Gate closed" } as Record<string, string>)[s.state]
        ?? s.status ?? "–";
    const where = [s.stand && `Stand ${s.stand}`, s.gate && `Gate ${s.gate}`, s.cargo && "Cargo"].filter(Boolean).join(" · ");
    out.push({
      key: `${s.flight}|${s.scheduled_at.getTime()}`,
      phase: s.actual_at || onGround || (s.state === "cancelled" && s.scheduled_at <= now) ? "past" : "next",
      sort: when.getTime(), time: scheduled(s.scheduled_at), flight_iata: s.flight, callsign: s.callsign ?? s.flight,
      other: s.other, usual: null, status: where ? `${status} · ${where}` : status,
    });
  }
  return out.sort((a, b) => a.sort - b.sort);
}

// Past flights greyed out and trimmed to the latest few; coming up in full, since that is
// what most readers want.
function Board({ rows, dir, iata }: { rows: BoardRow[]; dir: Dir; iata: string }) {
  const [allPast, setAllPast] = useState(false);
  const past = rows.filter((r) => r.phase === "past");
  const next = rows.filter((r) => r.phase === "next");
  const shownPast = allPast ? past : past.slice(-PAST_SHOWN);
  const head = (title: string, extra?: ReactNode) => (
    <tr>
      <td colSpan={4} style={{ ...td, paddingTop: 14, fontSize: 12, fontWeight: 600, color: MUTED }}>
        {title}{extra}
      </td>
    </tr>
  );
  const row = (r: BoardRow, greyed: boolean) => {
    const cell = (style: CSSProperties): CSSProperties => (greyed ? { ...style, color: MUTED } : style);
    return (
      <tr key={r.key}>
        <td style={cell({ ...num, textAlign: "left", fontWeight: 600 })}>{r.time}</td>
        <td style={cell({ ...td, fontWeight: 600 })}><FlightCode iata={r.flight_iata} callsign={r.callsign} /></td>
        <td style={cell(td)}>{r.other ?? "–"}</td>
        <td style={cell(td)}>{r.status}</td>
      </tr>
    );
  };
  const none = <tr><td colSpan={4} style={{ ...td, color: MUTED }}>None</td></tr>;
  return (
    <div style={{ maxHeight: 640, overflowY: "auto" }}>
      <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
        <thead style={{ position: "sticky", top: 0, background: c("bg"), zIndex: 1 }}>
          <tr>
            <th style={th}>{iata} local</th>
            <th style={th}>Flight</th>
            <th style={th}>{dir === "inbound" ? "From" : "To"}</th>
            <th style={th}>Status</th>
          </tr>
        </thead>
        <tbody style={{ opacity: 0.6 }}>
          {head(`Past ${PAST_HOURS} hours`, past.length > PAST_SHOWN && (
            <>
              {` (${allPast ? past.length : `latest ${PAST_SHOWN} of ${past.length}`}) `}
              <button onClick={() => setAllPast(!allPast)}
                style={{ border: "none", background: "none", padding: 0, cursor: "pointer", color: BLUE, fontSize: 12, fontFamily: "inherit" }}>
                {allPast ? "Show fewer" : "Show all"}
              </button>
            </>
          ))}
          {shownPast.length === 0 ? none : shownPast.map((r) => row(r, true))}
        </tbody>
        <tbody>
          {head(`Next ${NEXT_HOURS} hours (${next.length})`)}
          {next.length === 0 ? none : next.map((r) => row(r, false))}
        </tbody>
      </table>
    </div>
  );
}

export default function AirportConditions() {
  const theme = useTheme();
  // Airports, busiest first.
  const airportsQ = useSQLQuery(`
    select a.icao, a.iata, a.name, a.timezone, a.notam_source, count(f.flight_id) as arrivals
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
      median(terminal_minutes)                        as median_terminal,
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

  // Every NOTAM for this aerodrome in the latest feed whose validity covers now: the same
  // rows fct_airport_conditions.notams_in_force counts.
  const notamsQ = useSQLQuery(`
    select
      notam_key, number, category, condition, has_schedule, schedule, is_permanent, is_estimated,
      raw_text,
      strftime(starts_at at time zone 'UTC', '%d %b %Y %H:%MZ') as from_txt,
      strftime(ends_at at time zone 'UTC', '%d %b %Y %H:%MZ')   as to_txt
    from "aviation"."marts"."fct_notams"
    where location = '${icao}'
      and is_current
    order by starts_at desc, number
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
      flight_number_iata,
      callsign,
      icao24,
      airline_name,
      coalesce(departure_iata, departure_icao)                as origin,
      departure_icao,
      flight_category,
      terminal_minutes,
      excess_terminal_minutes
    from a
    where day_local = (select max(day_local) from a)
    order by arrived_at desc
  `, ready);

  const worstQ = useSQLQuery(`
    select
      strftime(arrived_at at time zone '${tz}', '%d %b %H:%M') as arrived,
      flight_number_iata,
      trim(callsign) as callsign,
      icao24,
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
      flight_number_iata,
      callsign,
      icao24,
      airline_name,
      coalesce(arrival_iata, arrival_icao)                    as destination,
      arrival_icao,
      flight_category,
      departure_terminal_minutes,
      excess_departure_minutes
    from d
    where day_local = (select max(day_local) from d)
    order by departed_at desc
  `, ready);

  // Callsigns seen arriving at / departing from this airport in the last 30 days, with
  // their usual origin / destination. Flight numbers repeat daily, so a live aircraft
  // with one of these callsigns is very likely inbound / outbound now.
  //
  // usual_min is the flight's usual local time of day (minutes after midnight): the median
  // offset from its first observed time, wrapped so flights around midnight average right.
  // days_14 is how many of the last 14 local days it was seen: the board only predicts
  // flights that operate most days.
  const historyQ = useSQLQuery(`
    with seen as (
      select callsign, 'inbound' as dir, coalesce(departure_iata, departure_icao) as other, arrived_at as seen_at
      from "aviation"."marts"."fct_arrivals"
      where arrival_icao = '${icao}' and arrived_at >= now() - interval 30 day and callsign is not null
      union all
      select callsign, 'outbound', coalesce(arrival_iata, arrival_icao), departed_at
      from "aviation"."marts"."fct_departures"
      where departure_icao = '${icao}' and departed_at >= now() - interval 30 day and callsign is not null
    ),
    timed as (
      select *,
        hour(seen_at at time zone '${tz}') * 60 + minute(seen_at at time zone '${tz}') as m,
        arg_min(hour(seen_at at time zone '${tz}') * 60 + minute(seen_at at time zone '${tz}'), seen_at)
          over (partition by callsign, dir) as ref
      from seen
    )
    select
      callsign, dir, mode(other) as other, count(*) as n,
      (((any_value(ref) + median(((((m - ref + 720) % 1440) + 1440) % 1440) - 720)) % 1440) + 1440) % 1440
        as usual_min,
      count(distinct cast(seen_at at time zone '${tz}' as date)) filter (where seen_at >= now() - interval 14 day)
        as days_14
    from timed
    group by callsign, dir
  `, ready);

  const live = useLiveAircraft(icao);
  const schedule = useSchedule(icao);
  const schedIndex = useMemo(() => {
    const m = new Map<string, Sched[]>();
    for (const s of schedule.flights) if (s.callsign) m.set(s.callsign, [...(m.get(s.callsign) ?? []), s]);
    return m;
  }, [schedule.flights]);
  const airlineIata = useMemo(
    () => new Map(rowsOf(airlinesQ.data).map((r) => [String(r.icao), String(r.iata)])),
    [airlinesQ.data],
  );
  // QFA627 -> QF627, as stg_opensky_flights does; null for callsigns that do not map.
  const flightIata = (callsign: string | null) => {
    const m = callsign ? /^([A-Z]{3})0*(\d{1,4})$/.exec(callsign) : null;
    return m && airlineIata.has(m[1]) ? `${airlineIata.get(m[1])}${m[2]}` : null;
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
  const kpi = rowsOf(kpiQ.data)[0];
  const depKpi = rowsOf(depKpiQ.data)[0];

  const history = useMemo(() => {
    const m: History = new Map();
    for (const r of rowsOf(historyQ.data)) {
      const h = m.get(r.callsign) ?? {};
      h[r.dir as Dir] = { other: r.other ?? null, usual: N(r.usual_min), days: N(r.days_14) };
      m.set(r.callsign, h);
    }
    return m;
  }, [historyQ.data]);

  // Direction of each airborne aircraft at the last fix, by airport and transponder address.
  // An arrival stays inbound until it lands, though downwind legs and holds point it away
  // from the airport, and a departure stays outbound.
  const lastDir = useRef(new Map<string, "inbound" | "outbound">());
  const placed = useMemo((): Placed[] => {
    if (!wx) return [];
    const aLat = N(wx.lat), aLon = N(wx.lon);
    const now = live.at ?? new Date();
    // Median minutes inside 50 NM at this airport over 30 days; fallbacks until data builds up.
    const terminalArrMin = kpi?.median_terminal != null ? N(kpi.median_terminal) : 15;
    const terminalDepMin = depKpi?.median_minutes != null ? N(depKpi.median_minutes) : 10;
    return live.aircraft.map((a) => {
      const km = distKm(a.lat, a.lon, aLat, aLon);
      const h = a.callsign ? history.get(a.callsign) : undefined;
      // Angle between the aircraft's track and the bearing to the airport: 0 = heading
      // straight at it, 180 = straight away.
      const off = Math.abs(((N(a.track_deg) - bearingDeg(a.lat, a.lon, aLat, aLon)) + 540) % 360 - 180);
      // Beyond 30 NM, history must agree with geometry: a reused callsign flying away is
      // not inbound. Closer in, aircraft manoeuvre on approach and departure, so trust history.
      const near = km < 30 * 1.852;
      // Near the airport a clear descent or climb says more than the heading, which turns
      // away from the airport on downwind and in holds.
      const descending = near && a.vrate_fpm != null && a.vrate_fpm <= -300;
      const climbing = near && a.vrate_fpm != null && a.vrate_fpm >= 300;
      let dir: Placed["dir"] = "other";
      if (a.on_ground) dir = km < 8 ? "ground" : "other";
      else if (h?.inbound !== undefined && h?.outbound !== undefined) {
        dir = descending ? "inbound" : climbing ? "outbound" : off < 90 ? "inbound" : "outbound";
      }
      else if (h?.inbound !== undefined && (near || off < 110)) dir = "inbound";
      else if (h?.outbound !== undefined && (near || off > 70)) dir = "outbound";
      const key = `${icao}|${a.icao24}`;
      const prev = lastDir.current.get(key);
      // A descent near the airport overrides an earlier outbound: an arrival first seen
      // level on downwind, or a departure coming back. A climb never overrides inbound,
      // so a go-around stays an arrival.
      if (!a.on_ground && prev && h?.[prev] !== undefined) {
        dir = prev === "outbound" && descending && h?.inbound !== undefined ? "inbound" : prev;
      }
      if (dir === "inbound" || dir === "outbound") lastDir.current.set(key, dir);
      else lastDir.current.delete(key);
      const speed = N(a.speed_kt);
      const seen = dir === "inbound" ? h?.inbound : dir === "outbound" ? h?.outbound : undefined;
      // Minutes between the aircraft and the runway: at current ground speed to / from the
      // 50 NM ring, plus this airport's median time inside it (approach, holding, climb-out),
      // pro rata when already inside. Straight-line time alone reads 10-20 min early.
      const terminal = dir === "inbound" ? terminalArrMin : terminalDepMin;
      const legMin = speed < 60 ? null
        : km > TERMINAL_KM ? ((km - TERMINAL_KM) / (speed * 1.852)) * 60 + terminal
        : terminal * (km / TERMINAL_KM);
      const eta_min = dir === "inbound" ? legMin : null;
      // Inbound: ETA against usual arrival. Outbound: estimated take-off (now minus that
      // time) against usual departure.
      const at = legMin == null ? null : new Date(now.getTime() + (dir === "inbound" ? 1 : -1) * legMin * 60_000);
      const sched = dir === "inbound" || dir === "outbound" ? schedFor(schedIndex, a.callsign, dir, at ?? now) : null;
      // Against the airport's schedule where it publishes one: ETA against the scheduled
      // arrival, the airport's own departure time against the scheduled departure (an
      // estimated take-off would count the taxi as delay). Otherwise against the usual time.
      const delay_min = sched && dir === "inbound" && at ? (at.getTime() - sched.scheduled_at.getTime()) / 60_000
        : sched?.actual_at ? (sched.actual_at.getTime() - sched.scheduled_at.getTime()) / 60_000
        : seen && at ? wrapMinutes(minuteOfDay(at, tz) - seen.usual) : null;
      return {
        ...a, dir, sched,
        other: sched?.other ?? seen?.other ?? null,
        flight_iata: flightIata(a.callsign),
        label: flightIata(a.callsign) ?? a.callsign ?? a.icao24,
        dist_nm: km / 1.852,
        eta_min,
        usual: seen ? hhmm(seen.usual) : null,
        delay_min,
        rag: ragOf(delay_min),
        event_at: dir === "inbound" || dir === "outbound" ? at : null,
      };
    });
  }, [live.aircraft, live.at, history, wx, airlineIata, tz, kpi, depKpi, icao, schedIndex]);
  const count = (dir: Placed["dir"]) => placed.filter((a) => a.dir === dir).length;

  // Recognised flights on the feed, for the boards. An aircraft on the ground here is the
  // arrival or departure whose usual time is nearest now: arrivals up to 3 h after their
  // usual time (taxiing in, parked with the transponder on), departures within 3 h of theirs.
  const boardLive = useMemo((): BoardLive[] => {
    const nowMin = minuteOfDay(live.at ?? new Date(), tz);
    const rows: BoardLive[] = [];
    for (const a of placed) {
      if (!a.callsign) continue;
      const base = { callsign: a.callsign, label: a.label, flight_iata: a.flight_iata, on_ground: a.on_ground, dist_nm: a.dist_nm, aircraft: a, sched: a.sched };
      if (a.dir === "inbound" || a.dir === "outbound") {
        rows.push({ ...base, dir: a.dir, other: a.other, usual: a.usual, event_at: a.event_at, rag: a.rag,
          status_note: a.dir === "inbound" || a.delay_min != null ? ragText(a) : null });
        continue;
      }
      if (a.dir !== "ground") continue;
      const h = history.get(a.callsign);
      // On the airport's schedule: an arrival due or landed within 3 h, or a departure not
      // yet gone within 3 h of its time, whichever is nearer now.
      const now = live.at ?? new Date();
      const near = (s: Sched | null) => s != null && Math.abs(schedTime(s).getTime() - now.getTime()) <= PAST_HOURS * 3_600_000;
      const sIn = schedFor(schedIndex, a.callsign, "inbound", now);
      const sOut = schedFor(schedIndex, a.callsign, "outbound", now);
      const inOk = near(sIn), outOk = near(sOut) && sOut!.state !== "departed";
      if (inOk || outOk) {
        const gap = (s: Sched) => Math.abs(schedTime(s).getTime() - now.getTime());
        const dir: Dir = inOk && (!outOk || gap(sIn!) <= gap(sOut!)) ? "inbound" : "outbound";
        const s = (dir === "inbound" ? sIn : sOut)!;
        const late = dir === "outbound" ? Math.max(0, (now.getTime() - s.scheduled_at.getTime()) / 60_000) : null;
        rows.push({ ...base, sched: s, dir, other: s.other, usual: h?.[dir] ? hhmm(h[dir]!.usual) : null, event_at: null,
          rag: dir === "outbound" ? ragOf(late) : "unknown",
          status_note: late != null && late >= 15 ? `Late ${Math.round(late)} min` : null });
        continue;
      }
      const arr = h?.inbound ? wrapMinutes(nowMin - h.inbound.usual) : null;   // minutes since usual arrival
      const dep = h?.outbound ? wrapMinutes(h.outbound.usual - nowMin) : null; // minutes to usual departure
      const arrOk = arr != null && arr >= -30 && arr <= PAST_HOURS * 60;
      const depOk = dep != null && dep >= -PAST_HOURS * 60 && dep <= PAST_HOURS * 60;
      const dir: Dir | null = arrOk && (!depOk || Math.abs(arr!) <= Math.abs(dep!)) ? "inbound" : depOk ? "outbound" : null;
      if (!dir) continue;
      const seen = h![dir]!;
      // A departure still on the ground after its usual time is running late.
      const late = dir === "outbound" && dep! < 0 ? -dep! : null;
      rows.push({ ...base, dir, other: seen.other, usual: hhmm(seen.usual), event_at: null,
        rag: dir === "outbound" ? ragOf(late ?? 0) : "unknown",
        status_note: late != null && late >= 15 ? `Late ${Math.round(late)} min` : null });
    }
    return rows;
  }, [placed, history, live.at, tz, schedIndex]);
  const memory = useFeedMemory(icao, boardLive, live.at);
  // En route: recognised flights on the feed still to arrive or depart. Inbound first,
  // nearest ETA first; then outbound waiting on the ground, then airborne, nearest first.
  const enRouteOrder = (r: BoardLive) =>
    r.dir === "inbound" ? r.aircraft.eta_min ?? r.dist_nm : 2e6 + (r.on_ground ? 0 : 1 + r.dist_nm);
  const enRoute = boardLive.filter(isEnRoute).sort((a, b) => enRouteOrder(a) - enRouteOrder(b));
  const ragCount = (rag: Rag) => enRoute.filter((r) => r.rag === rag).length;
  const scheduleNote = (what: string) =>
    `Before and after En route: ${what} on the ${schedule.source} live board, passenger and cargo, from ${PAST_HOURS} hours ago to ${NEXT_HOURS} hours ahead, refreshed every 3 minutes. Times are scheduled, ${iata} local; status gives the airport's estimate or actual time. Flights on the live feed now are in En route.${schedule.error ? ` Last refresh failed (${schedule.error}).` : ""}`;
  const boards = useMemo(() => {
    const now = live.at ?? new Date();
    return {
      inbound: schedule.source ? scheduleBoard("inbound", schedule.flights, boardLive, memory, now, tz)
        : buildBoard("inbound", boardLive, memory, history, now, tz, flightIata),
      outbound: schedule.source ? scheduleBoard("outbound", schedule.flights, boardLive, memory, now, tz)
        : buildBoard("outbound", boardLive, memory, history, now, tz, flightIata),
    };
    // flightIata only changes with airlineIata.
  }, [boardLive, memory, history, live.at, tz, airlineIata, schedule]);

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
  const name = airport?.name;
  const notams = rowsOf(notamsQ.data);
  const notamSource = NOTAM_SOURCES[airport?.notam_source as string] ?? null;
  const arrivals = rowsOf(arrivalsQ.data);
  const departures = rowsOf(departuresQ.data);

  const page: CSSProperties = { fontFamily: SANS, color: INK, background: c("bg"), padding: 24, maxWidth: 1100 };
  const frame = (body: ReactNode) => (
    <div className="airport-conditions" data-theme={theme.dark ? "dark" : "light"} style={page}>
      <style>{THEME_CSS}</style>
      {body}
    </div>
  );
  const intro = (
    <>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <h1 style={{ fontSize: 22, fontWeight: 600, margin: 0 }}>Airport conditions</h1>
        <ThemeSwitch mode={theme.mode} onChange={theme.setMode} />
      </div>
      <p style={{ fontSize: 13, color: MUTED, margin: "4px 0 0" }}>
        Weather, observed traffic and excess terminal-area time (minutes within 50 NM beyond the
        airport's rolling median). Flight and chart times are the selected airport's local time;
        METAR and TAF times are UTC (Z); daily movement counts use UTC days. Flights without an
        IATA flight number show their ATC callsign, muted.
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
                  style={{ cursor: "pointer", background: r.icao === icao ? c("row-active") : undefined }}
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
    return frame(
      <>
        {intro}
        <Section title="Airport detail">
          <Empty>
            {airportsQ.isError
              ? `Could not load the airport list: ${String(airportsQ.error?.message ?? airportsQ.error)}`
              : "No airports in reference.airports. Run dbt seed."}
          </Empty>
        </Section>
      </>,
    );
  }

  return frame(
    <>
      {intro}

      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginTop: 40 }}>
        <select
          value={iata}
          onChange={(e) => setPicked(e.target.value)}
          style={{ fontSize: 15, fontWeight: 600, padding: "4px 8px", border: `1px solid ${RULE}`, borderRadius: 6, background: c("surface"), color: INK }}
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
        title="NOTAMs in force"
        note={`Full text of every NOTAM for ${icao} that is in force now, newest first${notamSource ? `, from the ${notamSource}` : ""}. Times are UTC; refreshed every 3 hours. A NOTAM with a schedule (D) item) is active only in the listed windows. For analysis, not flight planning.`}
      >
        {notamsQ.isLoading || conditionsQ.isLoading ? <Skeleton h={160} /> : notamsQ.isError ? (
          <Empty>Could not load NOTAMs: {String(notamsQ.error?.message ?? notamsQ.error)}</Empty>
        ) : wx?.notams_in_force == null ? (
          <Empty>
            {notamSource
              ? `No NOTAMs loaded for ${iata} yet from the ${notamSource}.`
              : `No NOTAM feed for ${iata}.`}
          </Empty>
        ) : notams.length === 0 ? (
          <Empty>No NOTAMs in force for {iata}.</Empty>
        ) : (
          <NotamList key={icao} rows={notams} />
        )}
      </Section>

      <Section
        title="Airspace"
        note="Live aircraft within 500 NM. Flights inbound to or outbound from this airport are coloured by delay status: green on time (under 15 min late), amber 15-44 min late, red 45 min or more, grey no usual time; other traffic is light grey. Lines are observed arrival (blue) and departure (orange) paths of tracked flights over the last 3 days, which trace the procedures in use. Dashed ring: 50 NM terminal area. The panel shows the latest METAR, including the surface wind. Click an aircraft for its details. Zoom out to see en route traffic."
      >
        {!wx ? <Skeleton h={400} /> : (
          <AirspaceMap
            lat={N(wx.lat)}
            lon={N(wx.lon)}
            wx={wx}
            tracks={tracks}
            live={placed}
            dark={theme.dark}
            iata={iata}
            tz={tz}
          />
        )}
        <p style={{ fontSize: 12, color: MUTED, margin: "8px 0 0" }}>
          {live.error
            ? `Live positions unavailable: ${live.error}.`
            : live.at
              ? `${placed.length} live aircraft from ${live.source || "ADS-B"} (${count("inbound")} inbound, ${count("outbound")} outbound, ${count("ground")} on the ground here), updated ${clockParts(live.at, tz).time} ${iata} time. Hover an aircraft for its flight, route, altitude and speed.`
              : "Loading live positions…"}
          {" "}{tracks.length} tracked paths. Official SID/STAR charts are not shown; no free procedure data covers these airports.
        </p>
      </Section>

      <Section
        title="En route"
        note="Flights within 500 NM that use this airport, in the air or on the ground here waiting to depart (arrivals on the ground are on the Arrivals board): what is happening now, between the past and coming-up flights on the boards below. Where the airport publishes a live schedule (Hong Kong), delay is against the scheduled time and the stand or gate is shown. Elsewhere there is no schedule, so delay is against the flight's usual time here over the last 30 days: ETA for inbound flights (current ground speed to the 50 NM ring, then the airport's median time inside it), estimated take-off for outbound ones (the same, backwards), and for a departure still on the ground, how long past its usual time it is. Inbound / outbound comes from the same 30 days of callsigns."
      >
        {!live.at ? <Skeleton h={160} /> : enRoute.length === 0 ? (
          <Empty>{live.error ? "No live positions." : `No inbound or outbound flights for ${iata} recognised right now.`}</Empty>
        ) : (
          <div style={{ maxHeight: 420, overflowY: "auto" }}>
            <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
              <thead style={{ position: "sticky", top: 0, background: c("bg") }}>
                <tr>
                  <th style={th}>Status</th>
                  <th style={th}>Flight</th>
                  <th style={th}>Route</th>
                  <th style={{ ...th, textAlign: "right" }}>Distance</th>
                  <th style={{ ...th, textAlign: "right" }}>Altitude</th>
                  <th style={{ ...th, textAlign: "right" }}>Speed</th>
                  <th style={{ ...th, textAlign: "right" }}>{schedule.source ? "Scheduled" : "Usual"}</th>
                  <th style={{ ...th, textAlign: "right" }}>{iata} local</th>
                </tr>
              </thead>
              <tbody>
                {enRoute.map((r) => {
                  const a = r.aircraft;
                  const inbound = r.dir === "inbound";
                  const color = RAG_COLORS[r.rag];
                  const hm = (d: Date) => clockParts(d, tz).time.slice(0, 5);
                  // Another day's flight shows its date.
                  const day = (d: Date) => clockParts(d, tz).date;
                  const hmDay = (d: Date) => day(d) === day(live.at ?? new Date()) ? hm(d) : `${hm(d)} ${day(d).split(" ").slice(1).join(" ")}`;
                  return (
                    <tr key={a.icao24}>
                      <td style={td}>
                        <span style={{ display: "inline-block", width: 10, height: 10, borderRadius: 5, background: color, marginRight: 6 }} />
                        <span style={{ color, fontWeight: 600 }}>
                          {r.on_ground ? `On the ground${r.sched?.gate ? ` · Gate ${r.sched.gate}` : ""}${r.status_note ? ` · ${r.status_note}` : ""}`
                            : `${ragText(a)}${inbound && r.sched?.stand ? ` · Stand ${r.sched.stand}` : ""}`}
                        </span>
                      </td>
                      <td style={{ ...td, fontWeight: 600 }}><FlightCode iata={r.flight_iata} callsign={r.callsign} icao24={a.icao24} /></td>
                      <td style={td}>{inbound ? `${r.other ?? "?"} → ${iata}` : `${iata} → ${r.other ?? "?"}`}</td>
                      <td style={num}>{Math.round(r.dist_nm)} NM</td>
                      <td style={num}>{r.on_ground ? "Ground" : `${a.alt_ft.toLocaleString()} ft`}</td>
                      <td style={num}>{a.speed_kt == null ? "–" : `${a.speed_kt} kt`}</td>
                      {r.sched ? (
                        <td style={num} title={inbound ? "Scheduled arrival" : "Scheduled departure"}>{hmDay(r.sched.scheduled_at)}</td>
                      ) : (
                        <td style={{ ...num, color: MUTED }} title={`Usual ${inbound ? "arrival" : "departure"}${schedule.source ? "; not on the airport's schedule" : ""}`}>{r.usual ?? "–"}</td>
                      )}
                      <td style={num}>
                        {r.on_ground || !r.event_at ? "–"
                          : inbound ? `ETA ${hm(r.event_at)} (${Math.round(a.eta_min ?? 0)} min)`
                          : `Took off ~${hm(r.event_at)}`}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <p style={{ fontSize: 12, color: MUTED, margin: "8px 0 0" }}>
          {enRoute.filter((r) => r.dir === "inbound" && !r.on_ground).length} inbound, {enRoute.filter((r) => r.dir === "outbound" && !r.on_ground).length} outbound,{" "}
          {enRoute.filter((r) => r.on_ground).length} on the ground here waiting to depart: {ragCount("green")} on time, {ragCount("amber")} amber, {ragCount("red")} red, {ragCount("unknown")} unknown. Flights that have not used {iata} in the last 30 days show as other traffic.
        </p>
      </Section>

      <Section title="Arrivals" note={schedule.source ? scheduleNote("arrivals") : `Before and after En route; there is no live schedule. Past (greyed) is arrivals on the ground here now or that the feed showed landing this visit, then regular flights (seen on ${REGULAR_DAYS} of the last 14 days) by their usual time; coming up is regular flights not yet within 500 NM, by their usual time. Times are ${iata} local; ~ marks an estimate.`}>
        {!live.at && !live.error ? <Skeleton h={240} /> : <Board rows={boards.inbound} dir="inbound" iata={iata} />}
      </Section>

      <Section title="Departures" note={schedule.source ? scheduleNote("departures") : `Before and after En route; there is no live schedule. Past (greyed) is departures the feed showed leaving this visit, then regular flights (seen on ${REGULAR_DAYS} of the last 14 days) by their usual time; coming up is regular flights not yet seen, by their usual time. Times are ${iata} local; ~ marks an estimate.`}>
        {!live.at && !live.error ? <Skeleton h={240} /> : <Board rows={boards.outbound} dir="outbound" iata={iata} />}
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
                <Tooltip {...tooltipProps} />
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
              <Tooltip {...tooltipProps} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Bar dataKey="arrivals" name="Arrivals" fill={BLUE} />
              <Bar dataKey="departures" name="Departures" fill={c("blue-soft")} />
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

      <details style={{ marginTop: 20 }}>
        <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 600 }}>
          {arrivals.length ? `All arrivals, ${arrivals[0].day} (${iata} local)` : "All arrivals"}, with terminal time
        </summary>
        <p style={{ fontSize: 12, color: MUTED, margin: "4px 0 12px" }}>
          Every arrival OpenSky observed on the latest local day with data, from any origin, latest first. Terminal time only where the flight's track was fetched and well covered.
        </p>
        {arrivalsQ.isLoading ? <Skeleton h={200} /> : arrivals.length === 0 ? (
          <Empty>No OpenSky arrivals at {iata} yet.</Empty>
        ) : (
          <div style={{ maxHeight: 420, overflowY: "auto" }}>
            <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
              <thead style={{ position: "sticky", top: 0, background: c("bg") }}>
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
                    <td style={td}><FlightCode iata={r.flight_number_iata} callsign={r.callsign} icao24={r.icao24} /></td>
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
          {arrivals.length} arrivals. Muted flights have no IATA flight number and show their ATC callsign. Hover a flight for its callsign, an origin for its ICAO code.
        </p>
      </details>

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
                  <td style={td}><FlightCode iata={r.flight_number_iata} callsign={r.callsign} icao24={r.icao24} /></td>
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

      <details style={{ marginTop: 20 }}>
        <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 600 }}>
          {departures.length ? `All departures, ${departures[0].day} (${iata} local)` : "All departures"}, with terminal time
        </summary>
        <p style={{ fontSize: 12, color: MUTED, margin: "4px 0 12px" }}>
          Every departure OpenSky observed on the latest local day with data, to any destination, latest first. Terminal time only where the flight's track was fetched (tracks are fetched for flights landing at one of the five airports).
        </p>
        {departuresQ.isLoading ? <Skeleton h={200} /> : departuresQ.isError ? (
          <Empty>Could not load departures: {String(departuresQ.error?.message ?? departuresQ.error)}</Empty>
        ) : departures.length === 0 ? (
          <Empty>No OpenSky departures from {iata} yet.</Empty>
        ) : (
          <div style={{ maxHeight: 420, overflowY: "auto" }}>
            <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
              <thead style={{ position: "sticky", top: 0, background: c("bg") }}>
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
                    <td style={td}><FlightCode iata={r.flight_number_iata} callsign={r.callsign} icao24={r.icao24} /></td>
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
          {departures.length} departures. Muted flights have no IATA flight number and show their ATC callsign. Hover a flight for its callsign, a destination for its ICAO code.
        </p>
      </details>
    </>,
  );
}
