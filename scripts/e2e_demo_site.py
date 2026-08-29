"""Drives the public demo site (`frontend/demo-site/`) in a browser: category switching,
real images actually rendering, the license-plate category's blur actually being a blur
(not just a box drawn over a still-readable plate), keyboard navigation, and
`prefers-reduced-motion` disabling the auto-advance timer rather than just softening its
transition.

No login, no database bootstrap, no cleanup - this site has neither. Every image it
serves was already rendered and reviewed offline by scripts/build_demo_assets.py; this
script only checks that what got built is actually reaching a browser correctly.

    python scripts/e2e_demo_site.py [--headed]
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://demo.localhost:8080"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts", "ui-states")

# The plates showcase's flagship frame (d3a6aa09, confidence 0.637) - its real detected
# bbox, confirmed against the live model via build_demo_assets.find_plates(). Sampling
# inside this exact box, rather than guessing at pixel coordinates, is what makes the
# blur check a check of the real pipeline output and not just "an image is there."
FLAGSHIP_IMAGE = "plates/04.jpg"
FLAGSHIP_BBOX = (0.64045, 0.67304, 0.68559, 0.73333)  # x1, y1, x2, y2, normalised


def shot(page, name: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)
    print(f"    captured {name}.png")


def check(condition: bool, description: str, failures: list[str]) -> None:
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


# Draws the given <img> to an offscreen canvas and returns the variance of greyscale
# luminance inside `box` (normalised 0..1 against the image's own natural size) and, for
# comparison, inside an equal-size box shifted just outside it. A real blur collapses
# high-frequency detail - the kind a licence plate's characters are made of - so a
# genuinely blurred region reads a markedly lower variance than the untouched pixels
# right next to it. Runs inside the page, not this script, since it needs canvas.
_VARIANCE_JS = """
([selector, box]) => {
  const img = document.querySelector(selector);
  const canvas = document.createElement('canvas');
  canvas.width = img.naturalWidth;
  canvas.height = img.naturalHeight;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0);

  function variance(x1, y1, x2, y2) {
    const px1 = Math.max(0, Math.round(x1 * canvas.width));
    const py1 = Math.max(0, Math.round(y1 * canvas.height));
    const px2 = Math.min(canvas.width, Math.round(x2 * canvas.width));
    const py2 = Math.min(canvas.height, Math.round(y2 * canvas.height));
    const w = Math.max(1, px2 - px1);
    const h = Math.max(1, py2 - py1);
    const data = ctx.getImageData(px1, py1, w, h).data;
    const lumas = [];
    for (let i = 0; i < data.length; i += 4) {
      lumas.push(0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]);
    }
    const mean = lumas.reduce((a, b) => a + b, 0) / lumas.length;
    return lumas.reduce((a, b) => a + (b - mean) ** 2, 0) / lumas.length;
  }

  const [x1, y1, x2, y2] = box;
  const w = x2 - x1, h = y2 - y1;
  const inside = variance(x1, y1, x2, y2);
  // Shifted one box-width to the right, same size - still on/near the same vehicle,
  // never blurred, so it stands in for "detail this sharp is normal here."
  const outside = variance(x1 + w, y1, x2 + w, y2);
  return { inside, outside };
}
"""


def main() -> int:
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv)
        page = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
        page.set_default_timeout(20000)

        try:
            print("\n[1] Load the site")
            page.goto(BASE)
            page.wait_for_selector(".showcase-frame", timeout=15000)
            check(page.get_by_text("See it detect, on real footage").count() > 0, "hero renders", failures)
            tabs = page.locator(".tab")
            tab_count = tabs.count()
            check(tab_count >= 2, f"at least 2 category tabs render ({tab_count} found)", failures)
            check(
                page.get_by_role("button", name="Vehicle & Object Detection").count() > 0,
                "the vehicle/object category tab is present", failures,
            )
            shot(page, "41-demo-site-home")

            print("\n[2] The first category's image actually loads")
            img = page.locator(".showcase-image").first
            img.wait_for(state="visible")
            natural_width = img.evaluate("el => el.naturalWidth")
            check(natural_width > 0, f"the showcase image has real pixels (naturalWidth={natural_width})", failures)

            print("\n[3] Keyboard navigation advances the carousel")
            frame = page.locator(".showcase-frame")
            frame.focus()  # not .click() - a click may land on a child (the <img>,
            # which isn't focusable) and never actually focus the container that owns
            # the keydown handler and the auto-advance pause.
            status_before = page.get_by_role("status").inner_text()
            frame.press("ArrowRight")
            page.wait_for_timeout(200)
            status_after = page.get_by_role("status").inner_text()
            check(status_before != status_after, f"ArrowRight changed the frame ({status_before!r} -> {status_after!r})", failures)

            print("\n[4] Switch to License Plate Detection")
            page.get_by_role("button", name="License Plate Detection").click()
            page.wait_for_timeout(300)
            check(
                page.get_by_text("Detected — and automatically redacted.").count() > 0,
                "the plate category's tagline renders", failures,
            )
            # Land on the flagship frame directly via its dot, rather than assuming
            # frame 1 and however many auto-advances have already happened.
            page.locator(".showcase-dot").nth(3).click()  # 04.jpg is the 4th dot
            page.wait_for_timeout(300)
            plate_img = page.locator(".showcase-image").first
            src = plate_img.evaluate("el => el.getAttribute('src')")
            check(FLAGSHIP_IMAGE in (src or ""), f"the flagship plate frame is showing ({src})", failures)

            print("\n[5] The plate region is actually blurred, not just boxed")
            variance = page.evaluate(_VARIANCE_JS, [".showcase-image", list(FLAGSHIP_BBOX)])
            inside, outside = variance["inside"], variance["outside"]
            check(
                inside < outside * 0.6,
                f"blurred region reads markedly lower detail than the untouched region beside it "
                f"(inside={inside:.1f}, outside={outside:.1f})",
                failures,
            )
            shot(page, "42-demo-site-plate-blurred")

            print("\n[6] prefers-reduced-motion stops auto-advance, not just the fade")
            page.emulate_media(reduced_motion="reduce")
            page.goto(BASE)
            page.wait_for_selector(".showcase-frame", timeout=15000)
            status_start = page.get_by_role("status").inner_text()
            page.wait_for_timeout(4500)  # longer than one ADVANCE_MS interval
            status_later = page.get_by_role("status").inner_text()
            check(
                status_start == status_later,
                f"the frame did not auto-advance while reduced motion is on "
                f"({status_start!r} == {status_later!r})",
                failures,
            )

            print("\n[7] Without reduced motion, it does auto-advance")
            page.emulate_media(reduced_motion="no-preference")
            page.goto(BASE)
            page.wait_for_selector(".showcase-frame", timeout=15000)
            status_start2 = page.get_by_role("status").inner_text()
            page.wait_for_timeout(4500)
            status_later2 = page.get_by_role("status").inner_text()
            check(
                status_start2 != status_later2,
                f"the frame did auto-advance with no reduced-motion preference "
                f"({status_start2!r} -> {status_later2!r})",
                failures,
            )

            print("\n[8] The closing CTA has a real, well-formed contact link")
            cta_href = page.locator(".cta-button").get_attribute("href")
            check(
                bool(cta_href) and cta_href.startswith("mailto:") and "@" in cta_href,
                f"the CTA button is a mailto: link to a real address ({cta_href})",
                failures,
            )

        finally:
            browser.close()

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - categories switch, real images render, plates are actually blurred "
          "(not just boxed), and the carousel respects keyboard nav and reduced motion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
