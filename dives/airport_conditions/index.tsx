// MotherDuck Dive: per-airport weather, traffic and arrival impact.
// Complements the Vercel dashboard (fleet-wide averages + NOTAM map) with a drill-down
// into one airport. Reads aviation.marts / aviation.reference only.
// Published by scripts/deploy_motherduck.py; preview locally with `motherduck dive watch`.
import { useMemo, type CSSProperties, type ReactNode } from "react";
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

const th: CSSProperties = {
  textAlign: "left", fontWeight: 500, color: MUTED, padding: "6px 12px 6px 0", borderBottom: `1px solid ${RULE}`,
};
const td: CSSProperties = { padding: "6px 12px 6px 0", borderBottom: `1px solid ${RULE}` };
const num: CSSProperties = { ...td, textAlign: "right", fontVariantNumeric: "tabular-nums" };

export default function AirportConditions() {
  // Airports, busiest first, so the default selection has data to show.
  const airportsQ = useSQLQuery(`
    select a.icao, a.name, count(i.icao24) as arrivals
    from "aviation"."reference"."airports" a
    left join "aviation"."marts"."fct_arrival_weather_impact" i on i.arrival_icao = a.icao
    group by all
    order by arrivals desc, a.icao
  `);
  const airports = rowsOf(airportsQ.data);
  const [picked, setPicked] = useDiveState<string>("airport", "");
  const icao = picked || (airports[0]?.icao as string) || "";
  // icao always comes from the airports seed, so it is safe to inline.
  const ready = { enabled: icao !== "" };

  const overviewQ = useSQLQuery(`
    select
      icao,
      count(*)                     as hours,
      avg(is_ifr::int)             as ifr_share,
      avg(is_gusty::int)           as gusty_share,
      avg(has_thunderstorm::int)   as ts_share,
      max(wind_gust_kt)            as max_gust_kt
    from "aviation"."marts"."fct_airport_weather_hourly"
    where hour_utc >= now() - interval 7 day
    group by icao
    order by icao
  `);

  const kpiQ = useSQLQuery(`
    select
      count(*)                                        as arrivals,
      median(excess_terminal_minutes)                 as median_excess,
      quantile_cont(excess_terminal_minutes, 0.9)     as p90_excess,
      avg(is_ifr::int) filter (where has_current_metar) as ifr_arrival_share
    from "aviation"."marts"."fct_arrival_weather_impact"
    where arrival_icao = '${icao}'
      and arrived_at >= now() - interval 30 day
  `, ready);

  const hourlyQ = useSQLQuery(`
    select
      strftime(hour_utc at time zone 'UTC', '%d %b %H:00') as label,
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

  const worstQ = useSQLQuery(`
    select
      strftime(arrived_at at time zone 'UTC', '%d %b %H:%M') as arrived,
      coalesce(trim(callsign), icao24) as callsign,
      departure_icao,
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
  const name = airports.find((a) => a.icao === icao)?.name;

  return (
    <div style={{ fontFamily: SANS, color: INK, padding: 24, maxWidth: 1100 }}>
      <h1 style={{ fontSize: 22, fontWeight: 600, margin: 0 }}>Airport conditions</h1>
      <p style={{ fontSize: 13, color: MUTED, margin: "4px 0 0" }}>
        Weather, observed traffic and excess terminal-area time (minutes within 50 NM beyond the
        airport's rolling median). All times UTC.
      </p>

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
                  onClick={() => setPicked(r.icao)}
                  style={{ cursor: "pointer", background: r.icao === icao ? "#f3f4f6" : undefined }}
                >
                  <td style={{ ...td, fontWeight: r.icao === icao ? 600 : 400 }}>{r.icao}</td>
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

      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginTop: 40 }}>
        <select
          value={icao}
          onChange={(e) => setPicked(e.target.value)}
          style={{ fontSize: 15, fontWeight: 600, padding: "4px 8px", border: `1px solid ${RULE}`, borderRadius: 6 }}
        >
          {airports.map((a) => (
            <option key={a.icao} value={a.icao}>{a.icao}</option>
          ))}
        </select>
        <span style={{ fontSize: 15, color: MUTED }}>{name}</span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 24, marginTop: 20 }}>
        {kpiQ.isLoading || !kpi ? <Skeleton h={56} /> : (
          <>
            <KPI label="Arrivals analysed, 30 days" value={String(N(kpi.arrivals))} />
            <KPI label="Median excess terminal time" value={mins(kpi.median_excess)} />
            <KPI label="90th percentile excess" value={mins(kpi.p90_excess)} />
            <KPI label="Arrivals in IFR / LIFR" value={pct(kpi.ifr_arrival_share)} />
          </>
        )}
      </div>

      <Section title="Wind and flight category, last 72 hours" note="Each cell in the strip is one hour's latest METAR.">
        {hourlyQ.isLoading ? <Skeleton h={240} /> : hourly.length === 0 ? (
          <Empty>No METARs for {icao} in the last 72 hours.</Empty>
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
          <Empty>No OpenSky movements for {icao} in the last 30 days.</Empty>
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
          <Empty>No arrivals at {icao} with a current METAR yet.</Empty>
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

      <Section title="Slowest arrivals, last 30 days" note="Most excess terminal-area time, with the weather when they landed.">
        {worstQ.isLoading ? <Skeleton h={200} /> : rowsOf(worstQ.data).length === 0 ? (
          <Empty>No benchmarked arrivals at {icao} yet. Each needs at least one earlier arrival in the 30-day baseline.</Empty>
        ) : (
          <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
            <thead>
              <tr>
                <th style={th}>Arrived</th>
                <th style={th}>Callsign</th>
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
                  <td style={td}>{r.callsign}</td>
                  <td style={td}>{r.departure_icao ?? "–"}</td>
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
    </div>
  );
}
