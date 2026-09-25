# Apti demo app v2: register-a-face, live monitoring, local history

## Context

Apti found the reskinned demo app (`mobile/apti`) didn't match what they had in mind and
sent a full exam-invigilation product plan (candidate registration, live camera/RTSP
monitoring, a rules engine, session reports, 2FA, admin roles). Brainstormed with the
owner (2026-09-25) to fit that idea to what this app actually is: a **public, anonymous,
nothing-stored** showcase — confirmed explicitly ("this for the demo app for all") and
confirmed the face-registration step must be **session-only, never persisted**
(no accounts, so no real PDPA exposure - the real, consented, audited enrollment path
already exists in the platform for paying tenants: `backend/tenant_api/app/api/faces.py`,
built earlier this session).

Given that constraint, most of the pasted plan doesn't fit a public demo (RTSP cameras,
a persisted candidate database, push notifications, PDF reports, admin roles all assume a
real signed-in account) and stays out of scope here - it already exists, or belongs, in
the real product (`mobile/app` + the tenant backend). What's kept is reinterpreted to work
session-only, on-device where possible, confirmed with the owner before writing this up.

**Alert rule feasibility, decided on fact not preference:** unknown/registered face, phone
detected, and multiple-people-in-frame are backed by models that exist today
(`insightface-buffalo-l-*`, `yolov8n-general`). "Looking away" (head pose) and "face
covered" (mask/hand) have no model in the registry and are explicitly left out - not
faked. "Camera tampered" is a lightweight on-device brightness/motion heuristic, not an AI
model.

## Design

**Nav:** bottom tabs `Home | Detect | History` (no `Profile` tab - no accounts means
nothing to manage; a small settings affordance on Home covers clear-history/about/version,
matching the plan's "App" settings section minus everything account-shaped and minus dark
mode - the app is light-only by design, per the theme work already done this session).

**Home** - greeting + tagline, a "Models ready" status chip (no camera-count chip - there
is no real site), three cards: **Register a Face**, **Try a Model** (today's screen,
renamed), **Live Monitoring** (renamed from "AI Monitoring", visually primary, matches the
plan's emphasis). Recent-detections list below, session-only; no Live/Demo tag split since
everything in this app is the demo.

**Register a Face** - same 4-step wizard shape as the plan (candidate-style details ->
capture 3-5 angles with live quality checks, reusing the plan's own checklist: one face,
centred, lit, not blurry, eyes open -> a consent-style notice explaining "used only for
this session, never stored, discarded when you close the app" -> review/save). "Save"
calls ai-runtime's existing `/internal/v1/infer-face` (through `demo_api`, which already
never persists images) once per photo, discards the photo, and keeps only the returned
512-d embedding in app memory (`src/faceGallery.ts`, a simple in-memory store - no
SecureStore, so it cannot survive a restart even accidentally). Duplicate-face warning
("looks like X, 92% - continue?") is cosine similarity against gallery entries already
held in memory - the same match function already written and tested for the real product,
`backend/shared/csense_shared/faces/matching.py`, ported client-side in TypeScript since
this runs entirely in the app with no server round trip for matching itself.

**Try a Model** - today's screen (`App.tsx`, unchanged), plus a new **Face Recognition**
mode: the live loop calls `/internal/v1/infer-face` instead of `/internal/v1/infer`,
matches each returned embedding against the in-memory gallery client-side, and labels
boxes by name (or "Unknown") instead of a generic "Face" - a real, working feature, not a
scripted demo.

**Live Monitoring** - a continuous watch screen using the phone's own camera (no RTSP, no
accounts): session timer, running alert count, a live event feed (newest first,
severity-coloured chip per event, matching the plan's severity language). Runs the four
real rules concurrently each capture cycle (face match/unknown, phone, person-count,
tamper heuristic) rather than one mode at a time - the one place this app genuinely
combines models, which is the point of the screen. Stop shows a simple summary (duration,
alert counts by type) - no PDF export, no clip recording.

**History** - a local, on-device-only list (AsyncStorage-backed, capped e.g. 200 entries,
oldest dropped) of this session's/device's detections across Try-a-Model and Live
Monitoring, filterable by type/severity, tap for detail (thumbnail, confidence, time), and
a Clear-all action. The empty state and a persistent note both say plainly this list is
local to the device and never uploaded.

**Explicitly out of scope** (real product, not this app): login/2FA, RTSP cameras, a
persisted candidate database, push notifications, PDF reports, admin roles, data-retention
settings.

## Critical files

`mobile/apti/App.tsx` (today's single-file screen; likely splits into `app/(tabs)/
{home,detect,history}.tsx`-style files as it grows - a genuine case for breaking it up,
not premature structure); `mobile/apti/src/api.ts` (add `detectFaces` hitting
`/internal/v1/infer-face` via `demo_api`); `mobile/apti/src/faceGallery.ts` (new, in-memory
gallery + cosine match, ported from `backend/shared/csense_shared/faces/matching.py`);
`mobile/apti/src/components/ui.tsx`, `src/theme.ts` (reused, no changes needed); local
history storage via `@react-native-async-storage/async-storage` (new dependency).

`backend/demo_api/app/main.py` - needs a new route proxying ai-runtime's
`/internal/v1/infer-face` the same way `/detect` proxies `/infer` today (same rate limit,
same in-flight cap, same "nothing stored or logged" stance - the face embedding is
computed and returned, never written anywhere server-side, exactly like the real
enrollment path already treats photos).

## Verification

- Unit: the ported cosine-match function against known vectors (mirror the existing
  Python test's cases in `backend/tests/test_face_matching.py`).
- Manual, on a real device (per the earlier build's own gap - nothing in this app has been
  run on real hardware yet): register a real face, confirm Face Recognition mode names it
  correctly and labels a stranger "Unknown"; run Live Monitoring and confirm phone/unknown-
  face/multi-person alerts fire and appear in History; force-close and reopen the app and
  confirm the registered face is gone (session-only held) while History (device-local)
  survives; confirm the tamper heuristic fires when the lens is covered.
- Confirm `demo_api`'s new face route still enforces the same per-IP rate limit and
  in-flight cap as `/detect`, and logs no image data.
