/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Supabase project URL and publishable (or legacy anon) key. Without them, sign-in is
  // hidden and settings stay in the browser (src/account).
  readonly NEXT_PUBLIC_GROUNDKIT_SUPABASE_URL?: string;
  readonly NEXT_PUBLIC_GROUNDKIT_SUPABASE_PUBLISHABLE_KEY?: string;
}
