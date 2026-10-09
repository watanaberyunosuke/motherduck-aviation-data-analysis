/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Supabase project URL and publishable (or legacy anon) key. Without them, sign-in is
  // hidden and settings stay in the browser (src/account).
  readonly VITE_SUPABASE_URL?: string;
  readonly VITE_SUPABASE_PUBLISHABLE_KEY?: string;
}
