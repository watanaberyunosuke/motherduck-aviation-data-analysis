// The bar above the dashboard: Customise (works signed out, saved in this browser) and
// sign-in, registration and the account (when Supabase is configured).
import { useEffect, useId, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import type { Provider } from "@supabase/supabase-js";
import {
  HOME_TZ, formatLayout, parseLayout, type LayoutItem,
} from "../../../dives/airport_conditions/index.tsx";
import { useSQLQuery } from "../dive-runtime";
import { returnUrl, supabase } from "./supabase";
import {
  clearAuthError, deleteAccount, endRecovery, saveDisplayName, setSetting, signOut, sync,
  useAccount, useSetting,
} from "./store";

const MIN_PASSWORD = 8;

type Panel = null | "auth" | "account" | "customise";

// ?account=signin or ?account=signup (the intro website's links) opens that dialog once.
function linkedAuthMode(): "signin" | "signup" | null {
  const mode = new URLSearchParams(window.location.search).get("account");
  if (mode !== "signin" && mode !== "signup") return null;
  const u = new URL(window.location.href);
  u.searchParams.delete("account");
  window.history.replaceState(null, "", u);
  return mode;
}

export default function AccountBar() {
  const account = useAccount();
  const [linked] = useState(linkedAuthMode);
  const [authStart, setAuthStart] = useState<"signin" | "signup">(linked ?? "signin");
  const linkHandled = useRef(false);
  const [panel, setPanel] = useState<Panel>(null);
  useEffect(() => {
    if (!linked || linkHandled.current || !account.ready) return;
    linkHandled.current = true;
    if (!account.user) setPanel("auth");
  }, [linked, account.ready, account.user]);
  // A provider error or a password-reset link opens the sign-in dialog.
  useEffect(() => {
    if (account.authError || account.recovery) setPanel("auth");
  }, [account.authError, account.recovery]);

  const name = account.displayName || account.user?.email || "Account";
  return (
    <div className="gk-bar">
      <button type="button" className="gk-link" onClick={() => setPanel("customise")}>Customise</button>
      {account.configured && account.ready && (
        account.user ? (
          <button type="button" className="gk-chip" onClick={() => setPanel("account")} title={account.user.email ?? undefined}>
            <span className="gk-avatar" aria-hidden="true">{initials(name)}</span>
            <span className="gk-chip-name">{name}</span>
          </button>
        ) : (
          <button type="button" className="gk-button gk-button-primary" onClick={() => setPanel("auth")}>Sign in</button>
        )
      )}
      <CustomiseDialog open={panel === "customise"} onClose={() => setPanel(null)} />
      {account.configured && (
        <>
          <AuthDialog open={panel === "auth"} startMode={authStart}
            onClose={() => { setPanel(null); setAuthStart("signin"); clearAuthError(); endRecovery(); }} />
          <AccountDialog open={panel === "account" && !!account.user} onClose={() => setPanel(null)} />
        </>
      )}
    </div>
  );
}

function initials(name: string): string {
  const words = name.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  return (words.length > 1 ? words[0][0] + words[1][0] : (words[0] ?? "?").slice(0, 2)).toUpperCase();
}

// Dialog --------------------------------------------------------------------------------

function Dialog({ open, onClose, title, children }: {
  open: boolean; onClose: () => void; title: string; children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} className="gk-dialog" aria-labelledby={titleId} onClose={onClose}
      onClick={(e) => { if (e.target === ref.current) onClose(); }}>
      <div className="gk-dialog-body">
        <div className="gk-dialog-head">
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="gk-icon-button" aria-label="Close" onClick={onClose}>×</button>
        </div>
        {open && children}
      </div>
    </dialog>
  );
}

function Alert({ children, tone = "error" }: { children: ReactNode; tone?: "error" | "info" }) {
  return <p className={`gk-alert gk-alert-${tone}`} role={tone === "error" ? "alert" : "status"}>{children}</p>;
}

// Sign in, register, reset ------------------------------------------------------------------

type AuthMode = "signin" | "signup" | "reset" | "sent" | "newPassword";

const PROVIDERS: { id: Provider; label: string; scopes?: string }[] = [
  { id: "apple", label: "Continue with Apple" },
  { id: "google", label: "Continue with Google" },
  // Microsoft (Entra ID) only returns an email address when asked for it.
  { id: "azure", label: "Continue with Microsoft", scopes: "email" },
];

function AuthDialog({ open, onClose, startMode }: { open: boolean; onClose: () => void; startMode: "signin" | "signup" }) {
  const account = useAccount();
  const [mode, setMode] = useState<AuthMode>(startMode);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sentWhat, setSentWhat] = useState("");

  useEffect(() => {
    if (account.recovery) setMode("newPassword");
  }, [account.recovery]);
  // Close once signed in (but not mid-way through setting a new password).
  useEffect(() => {
    if (open && account.user && mode !== "newPassword") onClose();
  }, [open, account.user, mode, onClose]);
  // Each opening starts on the requested form (a password-reset link starts on its own).
  useEffect(() => {
    if (open && !account.recovery) setMode(startMode);
    if (!open) { setError(null); setPassword(""); }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!supabase) return null;
  const auth = supabase.auth;

  const run = async (fn: () => Promise<string | null>) => {
    setBusy(true);
    setError(null);
    try {
      setError(await fn());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const withProvider = (p: (typeof PROVIDERS)[number]) => run(async () => {
    const { error } = await auth.signInWithOAuth({ provider: p.id, options: { redirectTo: returnUrl(), scopes: p.scopes } });
    return error?.message ?? null; // on success the browser leaves for the provider
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (mode === "signin") {
      void run(async () => {
        const { error } = await auth.signInWithPassword({ email, password });
        return error ? friendly(error.message) : null;
      });
    } else if (mode === "signup") {
      if (password.length < MIN_PASSWORD) return setError(`Use at least ${MIN_PASSWORD} characters for your password.`);
      void run(async () => {
        const { data, error } = await auth.signUp({
          email, password,
          options: { emailRedirectTo: returnUrl(), data: name.trim() ? { display_name: name.trim() } : undefined },
        });
        if (error) return friendly(error.message);
        if (!data.session) { setSentWhat("confirm"); setMode("sent"); }
        return null;
      });
    } else if (mode === "reset") {
      void run(async () => {
        const { error } = await auth.resetPasswordForEmail(email, { redirectTo: returnUrl() });
        if (error) return friendly(error.message);
        setSentWhat("reset");
        setMode("sent");
        return null;
      });
    } else if (mode === "newPassword") {
      if (password.length < MIN_PASSWORD) return setError(`Use at least ${MIN_PASSWORD} characters for your password.`);
      void run(async () => {
        const { error } = await auth.updateUser({ password });
        if (error) return friendly(error.message);
        endRecovery();
        onClose();
        return null;
      });
    }
  };

  const titles: Record<AuthMode, string> = {
    signin: "Sign in to GroundKit",
    signup: "Create your GroundKit account",
    reset: "Reset your password",
    sent: "Check your email",
    newPassword: "Choose a new password",
  };

  return (
    <Dialog open={open} onClose={onClose} title={titles[mode]}>
      {account.authError && <Alert>{account.authError}</Alert>}
      {mode === "sent" ? (
        <>
          <p className="gk-text">
            {sentWhat === "confirm"
              ? <>We sent a link to <strong>{email}</strong>. Open it to confirm your address, then you are signed in.</>
              : <>If <strong>{email}</strong> has an account, we sent it a link to choose a new password.</>}
          </p>
          <button type="button" className="gk-link" onClick={() => setMode("signin")}>Back to sign in</button>
        </>
      ) : (
        <>
          {(mode === "signin" || mode === "signup") && (
            <>
              <div className="gk-stack">
                {PROVIDERS.map((p) => (
                  <button key={p.id} type="button" className="gk-button gk-button-wide" disabled={busy} onClick={() => void withProvider(p)}>
                    {p.label}
                  </button>
                ))}
              </div>
              <div className="gk-divider"><span>or with email</span></div>
            </>
          )}
          <form className="gk-stack" onSubmit={submit} noValidate>
            {mode === "signup" && (
              <Field label="Name (optional)">
                <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" maxLength={80} />
              </Field>
            )}
            {mode !== "newPassword" && (
              <Field label="Email">
                <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required />
              </Field>
            )}
            {mode !== "reset" && (
              <Field label={mode === "newPassword" ? "New password" : "Password"}
                hint={mode === "signup" || mode === "newPassword" ? `At least ${MIN_PASSWORD} characters.` : undefined}>
                <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required
                  autoComplete={mode === "signin" ? "current-password" : "new-password"} minLength={mode === "signin" ? undefined : MIN_PASSWORD} />
              </Field>
            )}
            {error && <Alert>{error}</Alert>}
            <button type="submit" className="gk-button gk-button-primary gk-button-wide" disabled={busy}>
              {{ signin: "Sign in", signup: "Create account", reset: "Send reset link", newPassword: "Save password" }[mode]}
            </button>
          </form>
          <div className="gk-row gk-footer-links">
            {mode === "signin" && (
              <>
                <button type="button" className="gk-link" onClick={() => setMode("reset")}>Forgot password?</button>
                <button type="button" className="gk-link" onClick={() => setMode("signup")}>Create an account</button>
              </>
            )}
            {(mode === "signup" || mode === "reset") && (
              <button type="button" className="gk-link" onClick={() => setMode("signin")}>I have an account</button>
            )}
          </div>
          {mode === "signup" && (
            <p className="gk-hint">
              An account syncs your GroundKit settings between the dashboard and the apps. See the{" "}
              <a href="https://groundkit-intro-website.vercel.app/privacy" target="_blank" rel="noreferrer">privacy policy</a>.
            </p>
          )}
        </>
      )}
    </Dialog>
  );
}

// Supabase's messages are written for developers; a few read better reworded.
function friendly(message: string): string {
  if (/invalid login credentials/i.test(message)) return "That email and password do not match an account.";
  if (/email not confirmed/i.test(message)) return "Confirm your email first: open the link we sent you.";
  if (/user already registered/i.test(message)) return "That email already has an account. Sign in instead.";
  return message;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="gk-field">
      <span className="gk-label">{label}</span>
      {children}
      {hint && <span className="gk-hint">{hint}</span>}
    </label>
  );
}

// Account -------------------------------------------------------------------------------

const PROVIDER_NAMES: Record<string, string> = { email: "Email and password", apple: "Apple", google: "Google", azure: "Microsoft" };

function AccountDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const account = useAccount();
  const [name, setName] = useState(account.displayName ?? "");
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) { setName(account.displayName ?? ""); setSaved(false); setError(null); setConfirmDelete(""); }
  }, [open, account.displayName]);

  const user = account.user;
  if (!user) return null;
  const methods = [...new Set((user.identities ?? []).map((i) => PROVIDER_NAMES[i.provider] ?? i.provider))];

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    const err = await saveDisplayName(name);
    setBusy(false);
    setError(err);
    setSaved(!err);
  };

  const remove = async () => {
    setBusy(true);
    const err = await deleteAccount();
    setBusy(false);
    if (err) setError(err);
    else onClose();
  };

  return (
    <Dialog open={open} onClose={onClose} title="Your account">
      <dl className="gk-facts">
        <dt>Email</dt><dd>{user.email ?? "Not shared by the provider"}</dd>
        <dt>Signed in with</dt><dd>{methods.join(", ") || "Email and password"}</dd>
        <dt>Settings</dt>
        <dd>
          {account.syncing ? "Syncing…" : account.syncError ? `Not synced: ${account.syncError}` : "Synced with the GroundKit apps"}
          {account.syncError && <> <button type="button" className="gk-link" onClick={() => void sync()}>Try again</button></>}
        </dd>
      </dl>
      <form className="gk-stack" onSubmit={save}>
        <Field label="Name">
          <input value={name} onChange={(e) => { setName(e.target.value); setSaved(false); }} autoComplete="name" maxLength={80} />
        </Field>
        <div className="gk-row">
          <button type="submit" className="gk-button" disabled={busy}>Save name</button>
          {saved && <span className="gk-hint" role="status">Saved.</span>}
        </div>
      </form>
      {error && <Alert>{error}</Alert>}
      <div className="gk-row gk-section-gap">
        <button type="button" className="gk-button gk-button-wide" onClick={() => { void signOut(); onClose(); }}>Sign out</button>
      </div>
      <details className="gk-danger">
        <summary>Delete account</summary>
        <p className="gk-text">
          Deletes your account, profile and synced settings for good. Settings on your devices stay
          there. Type DELETE to confirm.
        </p>
        <div className="gk-row">
          <input aria-label="Type DELETE to confirm" value={confirmDelete} onChange={(e) => setConfirmDelete(e.target.value)} />
          <button type="button" className="gk-button gk-button-danger" disabled={busy || confirmDelete !== "DELETE"} onClick={() => void remove()}>
            Delete account
          </button>
        </div>
      </details>
    </Dialog>
  );
}

// Customise -----------------------------------------------------------------------------

function CustomiseDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const account = useAccount();
  const airport = useSetting<string>("airport") ?? "";
  const homeTz = useSetting<string>("dashboardHomeTimeZone") ?? HOME_TZ;
  const layout = useSetting<string>("dashboardLayout") ?? "";
  const items = parseLayout(layout);

  const airportsQ = useSQLQuery(`
    select icao, iata, name from "aviation"."reference"."airports" order by iata
  `, { enabled: open });
  const zones = useMemo(() => {
    const all = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
    return all.includes(homeTz) ? all : [homeTz, ...all];
  }, [homeTz]);

  const saveLayout = (next: LayoutItem[]) => setSetting("dashboardLayout", formatLayout(next) || undefined);
  const move = (i: number, by: number) => {
    const next = [...items];
    const [item] = next.splice(i, 1);
    next.splice(i + by, 0, item);
    saveLayout(next);
  };
  const toggle = (i: number) => saveLayout(items.map((s, j) => (j === i ? { ...s, hidden: !s.hidden } : s)));

  return (
    <Dialog open={open} onClose={onClose} title="Customise the dashboard">
      <p className="gk-hint">
        {account.user
          ? "Saved to your account. Your home airport is shared with the GroundKit apps."
          : account.configured
            ? "Saved in this browser. Sign in to keep it on every device and share your home airport with the apps."
            : "Saved in this browser."}
        {" "}A link with an airport in it still opens that airport.
      </p>
      <div className="gk-stack">
        <Field label="Home airport" hint="Opens when the link names no airport.">
          <select value={airport} onChange={(e) => setSetting("airport", e.target.value || undefined)}>
            <option value="">Busiest (default)</option>
            {(airportsQ.data ?? []).map((a) => (
              <option key={String(a.icao)} value={String(a.icao)}>{String(a.iata)}: {String(a.name)}</option>
            ))}
            {airport && !(airportsQ.data ?? []).some((a) => a.icao === airport) && <option value={airport}>{airport}</option>}
          </select>
        </Field>
        <Field label="Third clock" hint="Shown next to UTC and the airport's local time.">
          <select value={homeTz} onChange={(e) => setSetting("dashboardHomeTimeZone", e.target.value === HOME_TZ ? undefined : e.target.value)}>
            {zones.map((z) => <option key={z} value={z}>{z.replace(/_/g, " ")}</option>)}
          </select>
        </Field>
      </div>
      <fieldset className="gk-sections">
        <legend className="gk-label">Sections</legend>
        <ol>
          {items.map((s, i) => (
            <li key={s.id} className={s.hidden ? "gk-hidden" : undefined}>
              <label className="gk-check">
                <input type="checkbox" checked={!s.hidden} onChange={() => toggle(i)} />
                <span>{s.title}</span>
              </label>
              <span className="gk-row">
                <button type="button" className="gk-icon-button" aria-label={`Move ${s.title} up`} disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
                <button type="button" className="gk-icon-button" aria-label={`Move ${s.title} down`} disabled={i === items.length - 1} onClick={() => move(i, 1)}>↓</button>
              </span>
            </li>
          ))}
        </ol>
        <button type="button" className="gk-link" disabled={layout === ""} onClick={() => setSetting("dashboardLayout", undefined)}>
          Restore the default layout
        </button>
      </fieldset>
      <p className="gk-hint">The colour theme is the switch at the top of the dashboard{account.user ? "; it is synced too" : ""}.</p>
    </Dialog>
  );
}
