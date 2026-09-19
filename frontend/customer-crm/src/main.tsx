import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { detectBrandSlug, isApexHostname } from "./branding/slug";
import "./styles.css";

// Read once, before anything renders: every route in App.tsx is declared root-absolute
// (`/dashboard`, `/login`, ...) with no basename concept of its own, so
// `BrowserRouter`'s own `basename` prop is what turns `/eaigleye/dashboard` into a
// route match for `/dashboard` — no route path anywhere else needs to change. No
// slug-switching-mid-session machinery: nobody moves between brands in one browser
// session, and editing the URL's brand segment is a real navigation/reload, which is
// the correct behavior for this app.
const brandSlug = detectBrandSlug(window.location.pathname);
// Hostname, not path, decides the apex splash - both app.3rdi.in and the bare 3rdi.in
// have pathname "/" for their own bare root, so path alone can't tell them apart (see
// isApexHostname's own docs for the real bug this fixes).
const isApex = isApexHostname(window.location.hostname);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter basename={brandSlug ? `/${brandSlug}` : undefined}>
      <App brandSlug={brandSlug} isApex={isApex} />
    </BrowserRouter>
  </React.StrictMode>,
);
