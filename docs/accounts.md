# GroundKit accounts

- **Optional.** The apps and the dashboard work signed out. An account syncs settings between iOS, Android and the dashboard.
- **Sign-in:** email and password (with email confirmation and password reset) and Google. Apple and Microsoft are off (section 3).
- **Backend:** Supabase Auth, plus two Postgres tables in the same project (`supabase/migrations`). The aviation API (`api/index.py`) is unchanged and still needs no sign-in.
- **Sync:** a timestamp per setting. The server keeps whichever change is newer, so two devices editing at once do not overwrite each other's other settings.
- **Deletion:** in-app on all three clients. Deleting the auth user also deletes the profile and settings.

## 1. Data model

| Object | Purpose |
|---|---|
| `profiles` | `display_name` (80 characters or fewer). Users can read and rename only their own row. |
| `user_settings` | `settings` (flat JSON object), `stamps` (time each key last changed), `updated_at`. Users can read their own row; all writes go through `merge_settings`. |
| `merge_settings(patch, patch_stamps)` | Applies each key whose stamp is newer than the stored one and returns the merged row. A JSON `null` value removes the key. A missing or future stamp counts as now. Keys must match `^[A-Za-z][A-Za-z0-9]{0,63}$`. The patch is capped at 16 KB and the stored settings at 64 KB. If the account was deleted after the token was issued, it returns HTTP 401 (`PT401`). |
| `set_display_name(name)` | The same as updating `profiles.display_name`, for Android, whose HTTP client has no PATCH. |
| `delete_own_account()` | Deletes the caller's auth user. The profile and settings go with it (cascade). |
| `on_auth_user_created` | Creates the profile (name from sign-up or Google) and an empty settings row. |

Tests: `make test-supabase` runs the migration against a throwaway Postgres with a minimal stand-in for Supabase's `auth` schema and roles (`supabase/tests`). CI runs the same tests in the `supabase` job.

## 2. Synced settings (shared contract)

| Key | Type | iOS | Android | Dashboard |
|---|---|---|---|---|
| `airport` | ICAO string, e.g. `VHHH` | Airport | Airport (stored as IATA, mapped) | Default airport when the URL has none |
| `gloveMode` | boolean | Glove mode | Not used, kept | Not used, kept |
| `keepAwake` | boolean | Keep screen on | Keep screen on | Not used |
| `appearance` | `auto`, `sunset`, `light` or `dark` | Appearance | Theme | Not used |
| `windCautionKt` | integer 5 to 100 | Wind caution | Caution from gusts of | Not used |
| `windWarningKt` | integer 5 to 100 | Wind warning | High wind from | Not used |
| `dashboardTheme` | `auto`, `light` or `dark` | Not used | Not used | Colour theme |
| `dashboardHomeTimeZone` | IANA zone | Not used | Not used | Third clock (default Australia/Melbourne) |
| `dashboardLayout` | Section ids in display order; a leading `-` hides one | Not used | Not used | Section order and visibility (`SECTIONS` in the Dive) |

- **Device only:** age (health-related), the API address, the Android airline filter, and Health or Health Connect access.
- **Unknown keys:** a client never deletes a key it does not know, because it only sends keys it changed. This lets clients add keys independently.
- **First sign-in:** settings changed before accounts existed have no stamp. They are sent with the oldest possible stamp, so the account's value wins where one exists.
- **Default mismatch:** the high-wind default is 40 kt on iOS and 35 kt on Android. Defaults are not uploaded, so each device keeps its own default until someone changes the setting.

## 3. Supabase project setup

1. Create a project, then apply `supabase/migrations` with `supabase db push` or the SQL editor.
2. Authentication > URL Configuration ([docs](https://supabase.com/docs/guides/auth/redirect-urls)):
   - Site URL: `https://groundkit-dashboard.harrydatahub.com/` (email confirmation and password-reset links open the dashboard).
   - Redirect URLs: the Site URL, `groundkit://auth-callback` (iOS and Android provider sign-in) and `http://localhost:5173/` for local development.
3. Authentication > Providers > Email: keep "Confirm email" on, and set the minimum password length to 8 to match the clients.
4. **Google** ([docs](https://supabase.com/docs/guides/auth/social-login/auth-google)): a Web OAuth client with redirect URI `https://<project-ref>.supabase.co/auth/v1/callback`.
5. **Apple and Microsoft: off.** Sign in with Apple needs a paid Apple Developer Program membership, so both are removed from the clients and should stay disabled under Authentication > Providers. To bring them back, revert the `chore/disable-apple-microsoft-sign-in` changes in the three client repos and configure [Apple](https://supabase.com/docs/guides/auth/social-login/auth-apple) (Services ID, then bundle ID `com.harrydatahub.GroundKit`; the web secret key expires every 6 months) and [Microsoft](https://supabase.com/docs/guides/auth/social-login/auth-azure) (Entra ID app, tenant "common", `email` scope).

## 4. Client configuration

The project URL and publishable key are public. Without them, each client hides sign-in and keeps settings on the device.

| Client | Where |
|---|---|
| Dashboard | Vercel environment variables `NEXT_PUBLIC_GROUNDKIT_SUPABASE_URL`, `NEXT_PUBLIC_GROUNDKIT_SUPABASE_PUBLISHABLE_KEY` (build time; set by the Vercel Supabase integration) |
| iOS | `GKSupabaseURL`, `GKSupabaseKey` in `Config/Info.plist` |
| Android | `groundkit.supabaseUrl`, `groundkit.supabaseKey` in `gradle.properties` or `~/.gradle/gradle.properties` |

Provider sign-in uses PKCE on all clients ([Supabase PKCE flow](https://supabase.com/docs/guides/auth/sessions/pkce-flow)). iOS uses `ASWebAuthenticationSession` for Google. Android uses a Custom Tab, returning to `groundkit://auth-callback`. Session storage: the dashboard uses supabase-js (browser storage), iOS uses the Keychain (this device only), and Android encrypts with an Android Keystore key and excludes the session from backups.

## 5. Known gaps

- **Email delivery.** Without custom SMTP, Supabase only sends auth emails to members of the project's team ([docs](https://supabase.com/docs/guides/auth/auth-smtp)). Anyone else who signs up with email gets no confirmation link and cannot finish signing up, and password-reset emails do not arrive either. Set up custom SMTP (and GroundKit-branded templates) before others use email sign-up.
- **App Store sign-in rule.** With Google sign-in and no Sign in with Apple, the iOS app may need another option that meets App Review Guideline 4.8 before submission ([guidelines](https://developer.apple.com/app-store/review/guidelines/)). Not relevant until there is a paid membership.
- **Sign in with Apple token revocation** (only if Apple comes back). Apple asks apps to revoke tokens on account deletion ([TN3194](https://developer.apple.com/documentation/technotes/tn3194-handling-account-deletions-and-revoking-tokens-for-sign-in-with-apple)); this needs a server-side call, for example a Supabase Edge Function.
- **Not run on devices.** The iOS UI was not built in Xcode, and the Android UI was not run on a device or emulator. Their client and sync logic was tested against a local stand-in for the Supabase API (section 6).
- No multi-factor authentication. No organisation accounts or employer SSO (out of scope for now).

## 6. Verification done

- **Database:** migration tests on Postgres 16 (`make test-supabase`): merge rules, row-level security, grants, account deletion and `PT401`.
- **Dashboard:** end-to-end in Chromium against a local stand-in for Supabase Auth and PostgREST running the real migration. Covers registration, password sign-in, a Google round trip (PKCE), password reset, live layout changes, two-browser sync, theme sync, rename and deletion.
- **iOS:** the account, sync and client logic compiles under Swift 6.2 with the project's MainActor-default settings. 14 tests pass, including a two-device sync and an account deleted on another device.
- **Android:** `testDebugUnitTest` and `assembleDebug` pass. The 12 new tests include an integration test, skipped unless `GK_SUPABASE_TEST_URL` and `GK_SUPABASE_TEST_KEY` point at a test project.
