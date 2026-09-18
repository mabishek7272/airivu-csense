# Camera Obstruction & Glare Detection — Failure Mode and Effects Analysis

**Version:** 1.0
**Status:** Analysis — informs a future implementation pass, does not itself ship code
**Date:** 2026-09-18
**Scope:** The two camera health use cases named but deliberately not built in
`csense_shared/cameras/health.py` — `obstruction` and `glare/night-vision` — per
[CHECKLIST.md](../CHECKLIST.md)'s own recorded reasoning: both need a decoded video
frame to analyze, and the connectivity probe (`camera_probe.py`) deliberately avoids
that dependency. This document is the requested alternative to building detection code
against unvalidated thresholds: a structured analysis of what can actually go wrong,
how severe each failure is, how likely it is, and how well (or poorly) it's caught
today — used to decide *what* to build and in *what order*, not to guess thresholds
under time pressure.

## Why an FMEA instead of code

The prior attempt to reason about detection thresholds for this deployment's own
camera (`autotek-dorani-nvr.dyndns.org`, documented in `CLAUDE.md`) found real,
measured evidence that guessed thresholds fail silently: `yolov8n-general` on that
camera's night IR mainstream found objects at 0.09–0.21 confidence — a production
`min_confidence` of 0.5 detects nothing at night on this camera, and no validation
data exists to know what the right threshold actually is. Obstruction/glare detection
has the identical shape of risk (a brightness/variance/edge-density threshold guessed
without real footage from an obstructed or glared camera), so the same caution
applies. An FMEA doesn't remove the need for real validation footage eventually — it
identifies *which* failure modes are worth validating first, so that when real
footage does become available (a real pilot camera, a deliberately-obstructed test
shot), effort goes to the highest-risk gaps first rather than being spent evenly
across every conceivable failure mode.

## Methodology

Standard automotive/systems FMEA scoring, 1–10 scale for each axis:

- **Severity (S)** — how bad the consequence is if the failure mode occurs and is
  never caught. 10 = a real security incident goes completely unmonitored with no
  trace; 1 = cosmetic, no operational impact.
- **Occurrence (O)** — how often this failure mode is expected to actually happen in
  a real outdoor deployment (dust, weather, vandalism, wildlife, equipment drift), not
  how "interesting" it is to build detection for.
- **Detection (D)** — how well the *current* system (as it exists today, before any
  of this pass's work) catches this failure mode. 10 = nothing catches it until a
  human notices during incident review, possibly days later; 1 = caught immediately
  and automatically.
- **RPN = S × O × D.** Not a hard ranking (RPN arithmetic has well-known distortions
  when scores cluster), but a consistent way to sort "worth building first" from
  "can wait."

Every score below is a reasoned estimate grounded in this deployment's own real,
already-measured constraints (no GPU, RTSP-only reference NVR, H.265/IR imagery,
`camera_probe.py`'s existing connectivity-only scope) — not a fabricated benchmark.
Where a number depends on data this project doesn't have yet (a real obstructed-camera
sample, a real glare sample from the reference NVR), that's stated explicitly rather
than guessed and presented as fact.

## System context: what actually exists today

Two relevant facts, both confirmed by reading the real code, not assumed:

1. **`camera_probe.py`'s connectivity probe speaks RTSP directly and never decodes a
   frame.** `classify_health_events()` (`csense_shared/cameras/health.py`) can only
   ever see `reachable`, `elapsed_ms`, and the stream's own SDP-reported `framerate` —
   there is no pixel data available at this layer, and deliberately so (reversing that
   design for a health check alone was the reason obstruction/glare detection was
   deferred in the first place).
2. **A real, decoded frame already exists elsewhere, for free.** `pipeline_runtime/
   app/main.py`'s `run_one_camera_cycle()` — the actual detection loop, running once
   per configured cycle per camera (0.5–2 fps range depending on deployment tier, per
   `CLAUDE.md`'s own measured capacity table) — calls `grab_frame_for_assignment()`,
   which returns a decoded BGR `numpy.ndarray`, *before* JPEG-encoding it and sending
   it to `ai-runtime` for object detection (`infer_via_ai_runtime()`). This is a
   materially different situation than the connectivity probe: the expensive part
   (RTSP connect, decode) is already being paid for on every detection cycle, for a
   reason unrelated to health monitoring. A brightness/variance/edge-density
   computation on that same already-decoded frame is a few `numpy` operations —
   reasoned to be several orders of magnitude cheaper than the JPEG encode + HTTP
   round-trip + neural-net inference already happening in the same cycle, though this
   is an engineering estimate, not a measured number (no profiling has been done).

**This changes the recommendation below**: the original deferral reasoning
("reversing `camera_probe.py`'s RTSP-only design just for this would be the wrong
place") is correct as stated and should stand — but it doesn't mean obstruction/glare
analysis has no cheap home in this codebase. It has one, already built for a different
reason, in `pipeline_runtime`, not in `camera_probe.py`.

---

## FMEA: Obstruction

Scoring summary (S = Severity, O = Occurrence, D = Detection, RPN = S×O×D):

| # | Failure Mode | S | O | D | RPN |
|---|---|---|---|---|---|
| O1 | Full occlusion | 9 | 3 | 9 | **243** |
| O2 | Partial occlusion | 6 | 6 | 9 | **324** |
| O3 | Camera physically redirected | 8 | 2 | 10 | **160** |
| O4 | Progressive lens fouling | 4 | 5 | 7 | **140** |

**O1 — Full occlusion.** Lens fully covered (cloth, tape, spray paint, a hand held
over it, a bag placed over the housing).
- *Effect:* Complete loss of scene coverage. If this happens to a camera watching a
  restricted/exclusion zone, an intrusion during the occluded window is entirely
  unrecorded and unalerted — the scenario every other feature in this project (RLS,
  evidence masking, audit trails) exists to prevent has a wide-open gap at the sensor
  itself.
- *Cause:* Vandalism/tampering (the deliberate case a security system most needs to
  catch), accidental (a delivery box left leaning against the housing), maintenance
  work in the field of view. Occurrence 3 — uncommon but not rare over a
  multi-camera, multi-year deployment.
- *Current detection:* **None.** `camera_probe.py`'s RTSP handshake still succeeds
  (the stream is "online" — reachable and streaming, just streaming black/near-uniform
  frames). The only current signal is a human noticing during incident review,
  potentially days later. Detection 9.
- *Recommended action:* **Build first.** A full occlusion produces a near-zero-variance
  frame (nearly one flat color/brightness — the same statistical signature
  `test_pipeline_evidence.py`'s own `noisy_image()` fixture already leans on, for an
  unrelated check, elsewhere this session). This is the least ambiguous failure mode in
  this whole analysis: a real low-variance threshold, once validated against one real
  deliberately-covered test shot, should be reliable without needing footage of every
  possible obstruction.

**O2 — Partial occlusion.** Lens partly blocked (a spiderweb, an insect, dirt/leaf
debris, a finger-sized smudge).
- *Effect:* Reduced field of view. A subject entering through the blocked portion is
  missed; a subject elsewhere is still detected — and the gap is invisible to an
  operator glancing at a thumbnail.
- *Cause:* Environmental — spiderwebs and insects are extremely common on outdoor
  IR-illuminated housings (insects are drawn to IR light specifically at night), plus
  ordinary debris. Occurrence 6 — genuinely common.
- *Current detection:* **None**, same as O1. Detection 9.
- *Recommended action:* **Second priority, highest raw RPN in this analysis** (see
  Synthesis below for why it isn't built first despite that). Harder than O1: a
  partial low-variance region needs a *regional* check (variance within a sub-block of
  the frame, not the whole frame) — a real algorithmic step up, since a spiderweb
  across 5% of the frame should not read the same as a fully covered lens. Needs real
  test footage before a threshold is trusted; this deployment has none yet.

**O3 — Camera physically redirected.** Mount loosened, knocked, or deliberately
turned so the previously-monitored area is no longer in frame.
- *Effect:* The camera is fully functional and streaming a perfectly clear image — of
  the wrong scene. Every downstream feature (zone privacy masking, rule ROI
  evaluation, incident detection) keeps working exactly as designed, evaluating rules
  against geometry that no longer corresponds to reality. Arguably worse than full
  occlusion for a deliberate-tampering scenario, since nothing about the stream itself
  looks unhealthy.
- *Cause:* Vandalism (the deliberate case), wind/vibration loosening a mount over
  time, accidental impact during nearby work. Occurrence 2 — infrequent for a
  properly-mounted camera.
- *Current detection:* **None, and none proposed below either.** This is the one
  failure mode in this analysis that brightness/variance analysis fundamentally cannot
  catch, since the frame itself is perfectly healthy. Detection 10 (the maximum —
  nothing catches this today and nothing in this document's own recommendations would
  either).
- *Recommended action:* **Out of scope for this pass, named explicitly.** Detecting
  scene drift needs a reference comparison (this camera's own frame now vs. its own
  frame at install/calibration time — a perceptual-hash or structural-similarity diff
  against a stored baseline), a genuinely different mechanism from brightness/variance
  obstruction checks. Worth a future, separate analysis if this becomes a real
  operational concern — not bundled into "obstruction detection" as if the same check
  would catch it, since it would not.

**O4 — Progressive lens fouling.** Dust, water spots, or condensation building up
gradually over weeks.
- *Effect:* Slowly degrading image quality/contrast, not a discrete on/off event.
  Detection confidence on real subjects degrades gradually rather than failing
  cleanly — the same shape of problem already documented for this deployment's
  night-IR detection thresholds (low confidence, not zero).
- *Cause:* Weather exposure, housing seal degradation, lack of cleaning maintenance.
  Occurrence 5 — expected over a multi-month outdoor deployment lifetime.
- *Current detection:* Partially caught today, indirectly: `camera_health.py`'s
  existing `framerate`/`network` checks are unrelated, but a badly-fouled lens
  producing consistently low-confidence detections would show up as an unusually
  quiet camera in the existing incident/detection history — a human reviewing "this
  camera hasn't fired a real alert in weeks" might notice, but nothing automated flags
  it. Detection 7.
- *Recommended action:* **Low priority, needs a trend not a threshold.** A
  single-frame brightness/variance check doesn't distinguish "always been a bit dim"
  from "getting dimmer" — this needs a rolling baseline comparison (this camera's own
  recent history vs. today), a materially different, longer-horizon feature than
  O1/O2's single-frame checks. Reasonable to defer past the first pass.

## FMEA: Glare / Exposure

| # | Failure Mode | S | O | D | RPN |
|---|---|---|---|---|---|
| G1 | IR reflector glare at night | 6 | 5 | 9 | **270** |
| G2 | Daytime sun/headlight glare | 5 | 6 | 8 | **240** |
| G3 | Backlight / high dynamic range | 3 | 5 | 6 | **90** |
| G4 | Auto-exposure hunting/oscillation | 5 | 3 | 9 | **135** |
| G5 | IR-cut filter stuck | 6 | 2 | 8 | **96** |

**G1 — IR reflector glare at night.** The camera's own IR illuminator reflecting off
a nearby reflective surface (glass, wet pavement, a spiderweb close to the lens, a
light-colored wall too near the housing).
- *Effect:* Localized overexposure that can both mask real subjects near the glare
  and, per this deployment's own already-measured night-IR detection quality
  (0.09–0.21 confidence, `CLAUDE.md`), interact badly with an already-marginal
  detection path. Also a known false-positive-alert source in outdoor security systems
  generally (bugs/webs near an IR emitter catching the light), though this platform's
  model-based (not motion-based) detection somewhat mitigates that specific sub-case.
- *Cause:* IR illuminator + nearby reflective surface, a mounting position too close
  to a wall/glass, a spiderweb directly in front of the IR emitter (the same
  insect-attraction mechanism as O2, positioned to catch the IR beam rather than the
  lens itself). Occurrence 5 — a real, common issue for IR-illuminated outdoor
  cameras; the reference NVR's own night imagery is IR greyscale, so this
  deployment's real hardware sits squarely in this failure mode's target case.
- *Current detection:* **None.** Detection 9.
- *Recommended action:* **Needs real validation data before anything ships.** This is
  the failure mode this document is most cautious about: this deployment's only real
  camera data point (`autotek-dorani-nvr`) already showed night-IR detection
  confidence is unvalidated and poor — adding a glare-brightness threshold on top of
  that, guessed rather than measured against a real IR-glare sample, repeats the exact
  mistake this whole document exists to avoid. Do not build a threshold for this row
  until a real IR-glare sample frame exists to validate against.

**G2 — Daytime sun/headlight glare.** Direct sun low on the horizon, or vehicle
headlights, saturating part of the sensor.
- *Effect:* Bright washout in the glare region; real subjects silhouetted or
  overexposed near the light source. Typically transient (minutes at sunrise/sunset,
  seconds for a passing vehicle), unlike G1's more persistent reflective-surface case.
- *Cause:* Camera orientation relative to sun path (a mounting/aiming decision, not a
  hardware fault), traffic/headlights for a camera facing a road or driveway.
  Occurrence 6 — near-universal for any outdoor camera not deliberately aimed away
  from the sun's path, twice daily around sunrise/sunset.
- *Current detection:* **None.** Detection 8.
- *Recommended action:* **Third priority, and needs a persistence requirement.**
  Unlike O1/O2, this failure mode is *expected* to be transient — a detector that
  fires a health alert every sunset would train operators to ignore camera-health
  alerts entirely, the same alert-fatigue risk this project's own quiet-hours/
  escalation design elsewhere is built to avoid. Any implementation must require the
  glare condition to persist across multiple consecutive detection cycles, not a
  single frame, before it's worth surfacing — a design requirement this analysis
  surfaced, not an implementation detail to figure out later.

**G3 — Backlight / high dynamic range.** Bright sky or light source behind a dark
subject; the subject is underexposed even though nothing is technically "broken."
- *Effect:* Real subjects present in the frame but poorly exposed, reducing detection
  confidence without any discrete "failure" to point at.
- *Cause:* Camera aiming relative to a bright background (sky, a window, a light
  fixture) — a positioning decision, not a fault. Occurrence 5 — common for any camera
  aimed toward open sky.
- *Current detection:* None, and arguably shouldn't be a "camera health" concern at
  all — closer to a scene-composition/installation-quality issue than a camera
  malfunction. Detection 6.
- *Recommended action:* **Out of scope, explicitly, not just by omission.** Flagged
  here so a reader can see it was considered and deliberately excluded, not missed:
  this is an installation/aiming quality signal, not a camera health failure, and
  conflating the two would make "camera health" alerts fire for cameras working
  exactly as their hardware allows, just poorly sited.

**G4 — Auto-exposure hunting/oscillation.** The camera's own auto-exposure repeatedly
cycling between bright and dark rather than settling, usually from a scene with two
very different lighting zones the AE algorithm can't reconcile.
- *Effect:* Detection confidence swings frame-to-frame even though no single frame is
  obviously "broken" — a genuinely hard case to catch with a single-frame brightness
  check, since any one frame in the cycle might look fine in isolation.
- *Cause:* Scene composition combined with a lower-end camera's AE algorithm quality;
  more common on budget hardware. Occurrence 3 — plausible but not confirmed to
  actually occur on this deployment's real hardware; no measured evidence either way.
- *Current detection:* **None.** Detection 9.
- *Recommended action:* **Deferred, needs cross-frame comparison.** Detecting
  oscillation fundamentally requires comparing brightness across a short sequence of
  frames (a variance-of-means over N cycles), not a single-frame threshold — a
  materially different mechanism from every other row except O4. Worth revisiting once
  O1/O2 are built and there's a real frame-history mechanism already in place to
  extend.

**G5 — IR-cut filter stuck.** The mechanical day/night filter fails to switch,
leaving the camera in the wrong mode (washed-out pink/magenta color cast in daylight,
or no IR sensitivity at night).
- *Effect:* A specific, discrete hardware fault (not a scene/lighting condition like
  G2–G4) producing a whole-frame color-cast signature distinct from the other glare
  modes — arguably closer to a "camera health" fault in the traditional sense than any
  other row in this analysis.
- *Cause:* Mechanical filter actuator failure — an actual hardware defect, not
  environmental. Occurrence 2 — real but infrequent, a discrete component failure
  rather than a continuous environmental exposure.
- *Current detection:* **None.** Detection 8.
- *Recommended action:* **Worth a look once G1's validation-data problem is solved.**
  Unlike G1–G4, this failure mode has a genuinely distinct statistical signature (an
  abnormal color-channel balance, not just a brightness/variance extreme) that could
  plausibly be checked without G1's same "day-vs-night IR ambiguity" risk — still
  needs real footage from a filter-stuck camera to validate against, which this
  deployment doesn't have any more than it has IR-glare footage, so it stays gated on
  the same real-data blocker as G1.

---

## Synthesis: what this analysis actually recommends

Raw RPN ranking, highest first: **O2 (324) > G1 (270) > O1 (243) > G2 (240) > O3
(160) > O4 (140) > G4 (135) > G5 (96) > G3 (90).**

Build order deliberately does **not** follow that ranking directly — the methodology
section already flags RPN as "not a hard ranking," and the real, overriding factor
here is **validation-data availability**, not severity×occurrence×detection alone.
Building against a guessed threshold for a failure mode with no real footage to
validate it against is the exact mistake this document exists to avoid (the same
mistake already made once, and caught, for this deployment's night-IR detection
confidence). So the actual recommendation is tiered by *what can be validated with
data that's realistically obtainable soon*, not by RPN alone:

1. **Tier 1 — buildable now.** **O1 (full occlusion)** and **G2 (daytime sun/headlight
   glare)**. Both have a single-frame statistical signature (near-zero global
   variance for O1; a saturated brightness region for G2) that can be validated with
   ordinary daytime test footage — pointing any available camera at a covered lens, or
   at the sky, needs no special hardware access and no night-IR imagery. O1 also has
   the highest severity in the whole analysis (9) and the third-highest RPN; G2 has a
   real, near-universal occurrence (twice daily, every outdoor camera) but must ship
   with the persistence-across-cycles requirement this analysis surfaced, or it
   becomes a twice-daily false alert.
2. **Tier 2 — buildable with more validation effort, still without hardware
   constraints.** **O2 (partial occlusion, highest raw RPN in the analysis)** needs a
   *regional* (not global) variance check validated against a real partial-obstruction
   sample — more effort than O1, but no different hardware access needed, just a
   second test shot (a spiderweb or object covering part of a lens, not all of it).
   **G4 (exposure oscillation)** needs a multi-frame history mechanism to exist first
   (not yet built) before it can even be attempted, independent of any hardware
   access question.
3. **Tier 3 — blocked on this deployment's specific night-IR hardware, same
   constraint already recorded for detection thresholds.** **G1 (IR reflector glare,
   second-highest RPN in the whole analysis at 270)** and **G5 (IR-cut filter stuck)**
   both need real footage from this deployment's actual IR-illuminated night camera
   (or an equivalent) to validate against — the identical blocker already documented
   for night-IR detection confidence thresholds. High RPN, but validating a threshold
   here without that specific footage would repeat the exact mistake this document
   was written to avoid, so these stay gated on real hardware access rather than
   built against a guess.
4. **Tier 4 — lower priority, different mechanism (trend, not threshold).** **O4
   (progressive lens fouling)** needs a rolling baseline comparison, a longer-horizon
   feature than any single-frame check above. Reasonable to defer past the first pass
   regardless of hardware access.
5. **Tier 5 — explicitly out of scope for this category.** **O3 (camera physically
   redirected)** and **G3 (backlight/HDR)** are named and analyzed, not silently
   dropped, but excluded because they need a fundamentally different mechanism
   (baseline scene comparison for O3; nothing at all, by design, for G3 — it isn't
   really a health failure) than brightness/variance analysis can provide at all,
   regardless of validation data.

**Where the eventual implementation should live**: not `camera_probe.py` (the
original, correct reasoning for keeping that RTSP-only stands) — in
`pipeline_runtime/app/main.py`'s `run_one_camera_cycle()`, alongside the frame that's
already decoded there for detection, writing into the same `camera_health_events`
table `classify_health_events()` already populates, with a new `check_name` (the
column is already free-text and un-constrained specifically so "the check set grows,"
per its own migration 0049 reasoning). No new schema, no new decode path — an
extension of two mechanisms that already exist, once O1's threshold has real
validation footage behind it.

**What this document does not do**: it does not ship O1's threshold, or any other
row's. The next real step is obtaining one deliberately-obstructed test frame from a
real camera (this deployment's own reference NVR, or any camera available for a
30-second test) to validate a real variance threshold against — the same category of
step CLAUDE.md's own night-detection section already calls out as still needed before
trusting a threshold on this hardware.
