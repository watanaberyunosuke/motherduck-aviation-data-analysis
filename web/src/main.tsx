import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import AirportConditions from "../../dives/airport_conditions/index.tsx";
import AccountBar from "./account/AccountBar";
import { startThemeSync } from "./account/theme";
import "./account/account.css";

// Before the Dive mounts, so it opens in the viewer's saved theme.
startThemeSync();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AccountBar />
    <AirportConditions />
  </StrictMode>,
);
