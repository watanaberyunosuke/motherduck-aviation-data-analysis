import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import AirportConditions from "../../dives/airport_conditions/index.tsx";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AirportConditions />
  </StrictMode>,
);
