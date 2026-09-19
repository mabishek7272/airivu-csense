/** The bare-domain splash (3rdi.in with no brand slug in the URL) - deliberately just
 *  the 3RDI mark, nothing else. 3RDI is the parent company operating every white-label
 *  brand on this platform (eAIgleye, Apti SafeGuard, Airivu CSense); this page is not
 *  itself a white-labeled product surface, so it does not go through BrandProvider and
 *  carries no login form or navigation - a visitor here is expected to already know, or
 *  be given, their own brand's specific URL (3rdi.in/eaigleye, 3rdi.in/apti, ...).
 */
export function Landing3rdiPage() {
  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
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
    </main>
  );
}
