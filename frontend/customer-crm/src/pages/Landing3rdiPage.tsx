/** The bare-domain splash (3rdi.in with no brand slug in the URL). 3RDI is the parent
 *  company operating every white-label brand on this platform (eAIgleye, Apti SafeGuard,
 *  Airivu CSense); this page is not itself a white-labeled product surface, so it does
 *  not go through BrandProvider and carries no login form or per-brand navigation - a
 *  visitor here is expected to already know, or be given, their own brand's specific URL
 *  (3rdi.in/eaigleye, 3rdi.in/apti, ...).
 *
 *  The one link this page does carry - Edge - is deliberately not a brand's product
 *  surface either: it goes to edge.3rdi.in, a separate static portfolio site for edge
 *  hardware (Raspberry Pi, Jetson, etc.), which is why it's a plain external link rather
 *  than a route inside this app.
 */
export function Landing3rdiPage() {
  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 32,
        // Creamy white, not the app's own dark --bg token - deliberately not a
        // white-labeled surface (see this file's own docstring), and the logo's own
        // source PNG already has a white background baked in, so this avoids a visible
        // seam around it.
        background: "#FAF6EF",
      }}
    >
      <img
        src="/3rdi-logo.png"
        alt="3RDI"
        style={{ maxWidth: "min(480px, 80vw)", width: "100%", height: "auto" }}
      />
      <a
        href="https://edge.3rdi.in"
        style={{
          padding: "10px 28px",
          borderRadius: 999,
          border: "1px solid #98134E",
          color: "#98134E",
          fontWeight: 600,
          fontSize: 14,
          letterSpacing: "0.04em",
          textDecoration: "none",
          textTransform: "uppercase",
        }}
      >
        Edge
      </a>
    </main>
  );
}
