// The Supabase client, or null when the site is built without VITE_SUPABASE_URL and
// VITE_SUPABASE_PUBLISHABLE_KEY. Without them sign-in is hidden and settings stay in this
// browser.
import { createClient, type SupabaseClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const key = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY as string | undefined;

export const supabase: SupabaseClient | null = url && key
  ? createClient(url, key, {
    auth: { flowType: "pkce", detectSessionInUrl: true, persistSession: true, autoRefreshToken: true },
  })
  : null;

// Where providers and emails send the browser back to: this page without its query, so it
// matches Supabase's redirect allow list exactly (docs/accounts.md section 3). The saved
// home airport covers what an ?airport= link would have shown.
export function returnUrl(): string {
  return `${window.location.origin}${window.location.pathname}`;
}
