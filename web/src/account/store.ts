// Account and settings state for the dashboard: the signed-in user, their profile and the
// synced settings. Settings work signed out too (kept in this browser) and are merged into
// the account on sign-in. React reads it with useAccount() / useSetting().
import { useSyncExternalStore } from "react";
import type { User } from "@supabase/supabase-js";
import { supabase, returnUrl } from "./supabase";
import {
  EMPTY_LOCAL, adopt, change, clean, isValid, patchOf,
  type Local, type SettingKey, type SettingValue,
} from "./settings";

const STORAGE_KEY = "groundkit-settings";
const SYNC_DELAY_MS = 800;
const REFRESH_AFTER_MS = 60_000;

export type AccountState = {
  configured: boolean; // Supabase keys present
  ready: boolean; // session restored (or there is none)
  user: User | null;
  displayName: string | null;
  local: Local;
  syncing: boolean;
  syncError: string | null;
  recovery: boolean; // arrived from a password-reset email
  authError: string | null; // from a provider redirect
};

function readLocal(): Local {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null");
    if (raw && typeof raw === "object") {
      const values = clean(raw.values);
      const stamps = typeof raw.stamps === "object" && raw.stamps ? raw.stamps : {};
      const pending = Array.isArray(raw.pending) ? raw.pending.filter((k: unknown) => typeof k === "string") : [];
      return { values, stamps, pending };
    }
  } catch { /* storage blocked or corrupt: start empty */ }
  return EMPTY_LOCAL;
}

function writeLocal(local: Local) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(local));
  } catch { /* keep it for this visit only */ }
}

// Provider errors come back in the query string or the hash.
function redirectError(): string | null {
  const params = new URLSearchParams(window.location.search);
  const hash = new URLSearchParams(window.location.hash.slice(1));
  return params.get("error_description") ?? hash.get("error_description");
}

let state: AccountState = {
  configured: supabase !== null,
  ready: supabase === null,
  user: null,
  displayName: null,
  local: readLocal(),
  syncing: false,
  syncError: null,
  recovery: false,
  authError: redirectError(),
};
const listeners = new Set<() => void>();

function set(patch: Partial<AccountState>) {
  state = { ...state, ...patch };
  if (patch.local) writeLocal(state.local);
  listeners.forEach((l) => l());
}

export const getAccount = () => state;
const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => listeners.delete(l);
};

export const subscribeSettings = subscribe;

export function useAccount(): AccountState {
  return useSyncExternalStore(subscribe, getAccount);
}

export function useSetting<T extends SettingValue>(key: SettingKey): T | undefined {
  return useSyncExternalStore(subscribe, () => state.local.values[key] as T | undefined);
}

// Settings ------------------------------------------------------------------------------

let timer: ReturnType<typeof setTimeout> | undefined;
let lastSync = 0;

export function setSetting(key: SettingKey, value: SettingValue | undefined) {
  if (value !== undefined && !isValid(key, value)) throw new Error(`Invalid value for ${key}`);
  if (state.local.values[key] === value) return;
  set({ local: change(state.local, key, value, new Date()) });
  if (state.user) {
    clearTimeout(timer);
    timer = setTimeout(() => void sync(), SYNC_DELAY_MS);
  }
}

// A value chosen before settings were synced (it has no stamp): offered with the oldest
// stamp, so on sign-in the account's value wins where it has one.
export function offerSetting(key: SettingKey, value: SettingValue) {
  if (state.local.stamps[key] || !isValid(key, value)) return;
  const local = change(state.local, key, value, new Date(0));
  set({ local });
}

let inFlight: Promise<void> | null = null;

// Sends pending keys (possibly none, which just fetches) and adopts the merged result.
export function sync(): Promise<void> {
  if (!supabase || !state.user) return Promise.resolve();
  if (inFlight) return inFlight.then(() => (state.local.pending.length ? sync() : undefined));
  const client = supabase;
  inFlight = (async () => {
    set({ syncing: true });
    const { patch, patch_stamps } = patchOf(state.local);
    const { data, error } = await client.rpc("merge_settings", { patch, patch_stamps }).single();
    if (error) {
      set({ syncing: false, syncError: error.message });
      // The account was deleted elsewhere while this session's token was still valid.
      if (error.code === "PT401") void client.auth.signOut({ scope: "local" });
      return;
    }
    lastSync = Date.now();
    set({ syncing: false, syncError: null, local: adopt(state.local, patch_stamps, data as { settings: unknown; stamps: unknown }) });
  })().finally(() => { inFlight = null; });
  return inFlight;
}

// Account -------------------------------------------------------------------------------

async function loadProfile(user: User) {
  if (!supabase) return;
  const { data } = await supabase.from("profiles").select("display_name").eq("id", user.id).maybeSingle();
  if (state.user?.id === user.id) set({ displayName: (data?.display_name as string | null) ?? null });
}

export async function saveDisplayName(name: string): Promise<string | null> {
  if (!supabase || !state.user) return "Not signed in.";
  const value = name.trim().slice(0, 80) || null;
  const { error } = await supabase.from("profiles").update({ display_name: value }).eq("id", state.user.id);
  if (error) return error.message;
  set({ displayName: value });
  return null;
}

export async function signOut() {
  clearTimeout(timer);
  await supabase?.auth.signOut();
}

// Deletes the account and everything stored with it, then signs out. Settings stay in
// this browser, no longer pending.
export async function deleteAccount(): Promise<string | null> {
  if (!supabase || !state.user) return "Not signed in.";
  const { error } = await supabase.rpc("delete_own_account");
  if (error) return error.message;
  await supabase.auth.signOut({ scope: "local" });
  set({ local: { ...state.local, pending: [] } });
  return null;
}

export const clearAuthError = () => set({ authError: null });
export const endRecovery = () => set({ recovery: false });

if (supabase) {
  const client = supabase;
  client.auth.onAuthStateChange((event, session) => {
    const user = session?.user ?? null;
    const changedUser = user?.id !== state.user?.id;
    set({ user, ready: true, ...(event === "PASSWORD_RECOVERY" ? { recovery: true } : {}) });
    if (event === "SIGNED_OUT") {
      set({ displayName: null, syncError: null, local: { ...state.local, pending: [] } });
      return;
    }
    if (user && changedUser) {
      // Supabase calls this inside its auth lock; defer other auth calls past it.
      setTimeout(() => {
        void loadProfile(user);
        void sync();
      }, 0);
    }
  });
  // Tidy the address bar after a provider or email link has been handled.
  const u = new URL(window.location.href);
  if (u.searchParams.has("code") || u.searchParams.has("error_description") || u.hash.includes("error_description")) {
    void client.auth.getSession().finally(() => window.history.replaceState(null, "", returnUrl()));
  }
  // Pick up changes made on other devices when the tab comes back.
  const refresh = () => {
    if (state.user && Date.now() - lastSync > REFRESH_AFTER_MS) void sync();
  };
  window.addEventListener("focus", refresh);
  window.addEventListener("online", () => void sync());
}
