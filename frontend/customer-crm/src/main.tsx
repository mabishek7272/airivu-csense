import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { detectBrandSlug } from "./branding/slug";
import "./styles.css";

// Read once, before anything renders: every route in App.tsx is declared root-absolute
// (`/dashboard`, `/login`, ...) with no basename concept of its own, so
// `BrowserRouter`'s own `basename` prop is what turns `/eaigleye/dashboard` into a
// route match for `/dashboard` — no route path anywhere else needs to change. No
// slug-switching-mid-session machinery: nobody moves between brands in one browser
// session, and editing the URL's brand segment is a real navigation/reload, which is
// the correct behavior for this app.
const brandSlug = detectBrandSlug(window.location.pathname);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter basename={brandSlug ? `/${brandSlug}` : undefined}>
      <App brandSlug={brandSlug} />
    </BrowserRouter>
  </React.StrictMode>,
);
