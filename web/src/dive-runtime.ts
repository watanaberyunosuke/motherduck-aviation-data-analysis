// Stand-in for `@motherduck/react-sql-query`, so the Dive in dives/airport_conditions
// runs unchanged outside MotherDuck. vite.config.ts aliases the import to this file.
//
// Queries run in the browser on DuckDB-WASM. The MotherDuck token never reaches the
// browser: each table a query names ("aviation"."marts"."x") is fetched once as Parquet
// from /api/tables/<schema>.<table> (api/index.py, cached at Vercel's edge) and loaded
// into an in-memory database attached as `aviation`, so the Dive's SQL resolves as-is.
import { useCallback, useEffect, useState } from "react";
import * as duckdb from "@duckdb/duckdb-wasm";
// Only the WebAssembly-exceptions build (every current browser supports it), served
// from this deployment. The fallback "mvp" build would add another 39 MB.
import ehWasm from "@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url";
import ehWorker from "@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?url";

let dbPromise: Promise<duckdb.AsyncDuckDB> | null = null;

function database(): Promise<duckdb.AsyncDuckDB> {
  dbPromise ??= (async () => {
    const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), new Worker(ehWorker));
    await db.instantiate(ehWasm);
    const con = await db.connect();
    try {
      // Time-zone-aware timestamps (now() - interval, `at time zone`) need ICU, which the
      // WASM build fetches from DuckDB's extension CDN on first load.
      await con.query(`install icu; load icu; set TimeZone = 'UTC'`);
      await con.query(`attach ':memory:' as aviation`);
    } finally {
      await con.close();
    }
    return db;
  })();
  return dbPromise;
}

const loaded = new Map<string, Promise<void>>();
const TABLE_REF = /"aviation"\."(\w+)"\."(\w+)"/g;

// Load every table the query names that is not loaded yet. Concurrent queries naming
// the same table share one download.
function loadTables(db: duckdb.AsyncDuckDB, sql: string): Promise<void[]> {
  const names = new Set([...sql.matchAll(TABLE_REF)].map((m) => `${m[1]}.${m[2]}`));
  return Promise.all([...names].map((name) => {
    if (!loaded.has(name)) {
      const load = (async () => {
        const res = await fetch(`/api/tables/${name}`);
        if (!res.ok) {
          // api/index.py explains itself in FastAPI's {"detail": ...} body.
          const detail = await res.json().then((b) => b?.detail, () => undefined);
          throw new Error(detail ?? `${name}: HTTP ${res.status}`);
        }
        const file = `${name}.parquet`;
        await db.registerFileBuffer(file, new Uint8Array(await res.arrayBuffer()));
        const [schema, table] = name.split(".");
        const con = await db.connect();
        try {
          await con.query(`create schema if not exists aviation.${schema}`);
          await con.query(`create or replace table aviation.${schema}.${table} as select * from read_parquet('${file}')`);
        } finally {
          await con.close();
        }
      })();
      loaded.set(name, load);
      load.catch(() => loaded.delete(name)); // let a later query retry
    }
    return loaded.get(name)!;
  }));
}

// Arrow hands back BigInt for 64-bit integers; the Dive does arithmetic with Number().
const plain = (v: unknown) => (typeof v === "bigint" ? Number(v) : v);

async function run(sql: string): Promise<Record<string, unknown>[]> {
  const db = await database();
  await loadTables(db, sql);
  const con = await db.connect();
  try {
    const result = await con.query(sql);
    return result.toArray().map((row) => {
      const obj = row.toJSON() as Record<string, unknown>;
      for (const k of Object.keys(obj)) obj[k] = plain(obj[k]);
      return obj;
    });
  } finally {
    await con.close();
  }
}

type QueryState = {
  data: Record<string, unknown>[] | undefined;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
};

export function useSQLQuery(sql: string, options: { enabled?: boolean } = {}): QueryState {
  const enabled = options.enabled ?? true;
  // A disabled query reports loading: in the Dive it is only disabled until the airport
  // list arrives, and a skeleton reads better than an empty-state message.
  const [state, setState] = useState<QueryState>({ data: undefined, isLoading: true, isError: false, error: null });

  useEffect(() => {
    if (!enabled) return;
    let stale = false;
    setState((s) => ({ ...s, isLoading: true }));
    run(sql).then(
      (data) => !stale && setState({ data, isLoading: false, isError: false, error: null }),
      (error) => !stale && setState({ data: undefined, isLoading: false, isError: true, error }),
    );
    return () => { stale = true; };
  }, [sql, enabled]);

  return state;
}

// Dive state lives in the URL query string, so a link opens the same view.
export function useDiveState<T extends string>(key: string, initial: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(
    () => (new URLSearchParams(window.location.search).get(key) as T | null) ?? initial,
  );
  const set = useCallback((next: T) => {
    const url = new URL(window.location.href);
    url.searchParams.set(key, next);
    window.history.replaceState(null, "", url);
    setValue(next);
  }, [key]);
  return [value, set];
}
