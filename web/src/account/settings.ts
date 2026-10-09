// The synced settings shared by the dashboard and the iOS and Android apps. The contract
// (keys, types, merge rule) is docs/accounts.md section 3; keep the three clients in step.
//
// Each client keeps the values, the time each key last changed (its stamp) and the keys
// changed since the last successful sync (pending). Syncing sends the pending keys to
// merge_settings(), which keeps the newer stamp per key, and adopts what it returns.

export const SETTING_KEYS = [
  "airport", // home airport, ICAO (VHHH): the apps' airport and the dashboard's default
  "gloveMode",
  "keepAwake",
  "appearance", // apps: auto | sunset | light | dark
  "windCautionKt",
  "windWarningKt",
  "dashboardTheme", // auto | light | dark
  "dashboardHomeTimeZone", // IANA zone of the dashboard's third clock
  "dashboardLayout", // section order and visibility, see SECTIONS in the Dive
] as const;

export type SettingKey = (typeof SETTING_KEYS)[number];
export type SettingValue = string | number | boolean;
export type Settings = Partial<Record<SettingKey, SettingValue>>;
export type Stamps = Partial<Record<SettingKey, string>>;

const oneOf = (...allowed: string[]) => (v: unknown) => typeof v === "string" && allowed.includes(v);
const isBool = (v: unknown) => typeof v === "boolean";
const knots = (v: unknown) => typeof v === "number" && Number.isInteger(v) && v >= 5 && v <= 100;
const text = (max: number) => (v: unknown) => typeof v === "string" && v.length <= max;

const VALID: Record<SettingKey, (v: unknown) => boolean> = {
  airport: (v) => typeof v === "string" && /^[A-Z0-9]{3,4}$/.test(v),
  gloveMode: isBool,
  keepAwake: isBool,
  appearance: oneOf("auto", "sunset", "light", "dark"),
  windCautionKt: knots,
  windWarningKt: knots,
  dashboardTheme: oneOf("auto", "light", "dark"),
  dashboardHomeTimeZone: text(64),
  dashboardLayout: text(1000),
};

export const isSettingKey = (k: string): k is SettingKey => (SETTING_KEYS as readonly string[]).includes(k);
export const isValid = (k: SettingKey, v: unknown): v is SettingValue => VALID[k](v);

// Keeps known keys with valid values; anything else (a newer client's keys, bad data) is
// dropped on read but never deleted on the server, since only pending keys are sent.
export function clean(raw: unknown): Settings {
  const out: Settings = {};
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    for (const [k, v] of Object.entries(raw)) if (isSettingKey(k) && isValid(k, v)) out[k] = v;
  }
  return out;
}

export type Local = { values: Settings; stamps: Stamps; pending: SettingKey[] };
export const EMPTY_LOCAL: Local = { values: {}, stamps: {}, pending: [] };

// A change made on this client: the new value (undefined removes it), stamped now.
export function change(local: Local, key: SettingKey, value: SettingValue | undefined, now: Date): Local {
  const values = { ...local.values };
  if (value === undefined) delete values[key];
  else values[key] = value;
  return {
    values,
    stamps: { ...local.stamps, [key]: now.toISOString() },
    pending: local.pending.includes(key) ? local.pending : [...local.pending, key],
  };
}

// The arguments for merge_settings(): the pending keys, null for removed ones.
export function patchOf(local: Local): { patch: Record<string, SettingValue | null>; patch_stamps: Stamps } {
  const patch: Record<string, SettingValue | null> = {};
  const patch_stamps: Stamps = {};
  for (const k of local.pending) {
    patch[k] = local.values[k] ?? null;
    if (local.stamps[k]) patch_stamps[k] = local.stamps[k];
  }
  return { patch, patch_stamps };
}

// Adopt the server's merged settings. Keys changed again while the request was in flight
// (stamp differs from what was sent) stay local and pending, for the next sync.
export function adopt(local: Local, sent: Stamps, server: { settings: unknown; stamps: unknown }): Local {
  const values = clean(server.settings);
  const stamps: Stamps = {};
  if (server.stamps && typeof server.stamps === "object") {
    for (const [k, v] of Object.entries(server.stamps)) if (isSettingKey(k) && typeof v === "string") stamps[k] = v;
  }
  const pending = local.pending.filter((k) => local.stamps[k] !== sent[k]);
  for (const k of pending) {
    const v = local.values[k];
    if (v === undefined) delete values[k];
    else values[k] = v;
    if (local.stamps[k]) stamps[k] = local.stamps[k];
  }
  return { values, stamps, pending };
}
