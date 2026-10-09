// Keeps the Dive's colour theme in step with the dashboardTheme setting. The Dive keeps its
// own choice in localStorage and announces changes (THEME_CHANGED_EVENT); a synced change
// from another device is passed back to it (THEME_SET_EVENT).
import { THEME_CHANGED_EVENT, THEME_SET_EVENT } from "../../../dives/airport_conditions/index.tsx";
import { getAccount, offerSetting, setSetting, subscribeSettings } from "./store";

const DIVE_THEME_KEY = "airport-conditions-theme";
type Mode = "auto" | "light" | "dark";

// Before the Dive mounts, so it starts in the saved theme rather than switching.
export function startThemeSync() {
  try {
    const legacy = localStorage.getItem(DIVE_THEME_KEY);
    if (legacy === "light" || legacy === "dark") offerSetting("dashboardTheme", legacy);
  } catch { /* storage blocked */ }
  let last = getAccount().local.values.dashboardTheme as Mode | undefined;
  if (last) {
    try {
      if (last === "auto") localStorage.removeItem(DIVE_THEME_KEY);
      else localStorage.setItem(DIVE_THEME_KEY, last);
    } catch { /* storage blocked: the Dive starts on auto */ }
  }
  window.addEventListener(THEME_CHANGED_EVENT, (e) => {
    const mode = (e as CustomEvent).detail as Mode;
    last = mode;
    setSetting("dashboardTheme", mode);
  });
  subscribeSettings(() => {
    const mode = getAccount().local.values.dashboardTheme as Mode | undefined;
    if (mode && mode !== last) {
      last = mode;
      window.dispatchEvent(new CustomEvent(THEME_SET_EVENT, { detail: mode }));
    }
  });
}
