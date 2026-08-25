# Open Clarifications

Things that materially affect scope or correctness and need a human decision. None of
these block foundational engineering work, so I'm proceeding with the stated defaults and
will course-correct when answered.

| # | Topic | Question | Default I'm proceeding with |
|---|---|---|---|
| 1 | Legacy system | ~~Does a real legacy CSense codebase/database exist?~~ **ANSWERED 2026-08-25.** | Legacy system found at `/var/www/csense` on `103.118.158.92` (12 GB: FastAPI backend, SQLite `csense_users.db`, DDNS service, CsenseAgent, React frontend, PM2). Note `/home/ubuntu/csense` is a *different* thing — a prior partial attempt at this same modernization. Model estate has been migrated; the rest of Phase 7 is now unblocked. |
| 2 | Team/timeline | The source plan assumes ~20 people over 44 weeks. Actual execution here is solo/AI-driven. | Re-scoped checklist in [CHECKLIST.md](CHECKLIST.md); features build in the same phase order but without hardware-lab/pentest/production-rollout items that need people or contracts I can't supply. |
| 3 | Deployment target | Cloud provider / on-prem / region? | **Local Docker Compose** (confirmed by user). Kubernetes/cloud IaC deferred until a real target environment is chosen. |
| 4 | Pilot tenant & use cases | Who is the first real customer and which detection use cases matter most (intrusion, PPE, ANPR, fire/smoke, etc.)? | Building generic pipeline framework; reference pipeline will target a common case (restricted-zone intrusion) as the first working example. |
| 5 | Camera/NVR hardware | No physical cameras/NVRs available. | Will validate against RTSP test streams / simulated camera sources instead of a certified hardware matrix. |
| 6 | AI models | ~~Build in-house, license, or use open-source?~~ **ANSWERED.** | 14 models migrated from the legacy server (see §Model estate below). Licence exposure is real and unresolved — see #15 and #16. |
| 7 | Notification providers | No SMS/push/email provider contracted (Twilio, SendGrid, FCM, etc.). | Building a pluggable provider-adapter interface with a working email adapter (SMTP) and a console/log adapter for SMS/push so the pipeline is real and swappable once a provider is chosen. |
| 8 | Legal/privacy jurisdiction | CCTV and data-retention law varies by country; not specified. | Defaulting to conservative settings: masking-on-by-default, explicit retention policy fields, no default facial recognition — but **do not treat this as legal compliance advice**; a real deployment needs jurisdiction-specific legal review. |
| 9 | Billing system | External billing system referenced but unnamed. | Out of scope for release one per the PRD itself; building the entitlement/quota model so it can plug into a billing system later. |
| 10 | Git remote / CI runner | No GitHub/GitLab remote configured. | CI workflow files are written but won't actually run until a remote is added — tell me if/where to push. |
| 11a | Platform role assignment | docs/05_BACKEND_SCHEMA.md §5.9 defines `platform_developers` but not how a developer gets a platform-audience role (memberships are tenant-scoped and can't hold a NULL tenant_id). | Added a small `platform_role_assignments` table (developer ↔ role) as a spec-consistent extension — flagging for architecture review rather than treating it as silently decided. |
| 11 | Secrets management | No secret manager (Vault/AWS Secrets Manager/etc.) specified for local dev. | Using `.env` files (git-ignored) + documented rotation path for local dev; production secret store is a Phase 8/9 decision tied to #3. |

| 12 | Local openssl quirk | This machine's Git Bash `openssl` (mingw64 build) fails silently writing to a path containing an apostrophe (`kamal's loq`) when given an MSYS-style `/c/...` path. | Fixed in `scripts/generate_dev_secrets.sh` by converting to a Windows-style path via `cygpath -m` before calling openssl. Verified working. Worth knowing if you write other scripts that shell out to openssl/native Windows tools from this repo's path. |

| 13 | Auth route split | TRD §10.2 shows one illustrative `/api/v1/auth/**` path but also says (§6.3) gateway routing is by path prefix per API, and doesn't run a separate standalone Identity Service. | Customer auth stays at `/api/v1/auth/**` (Tenant API); platform auth moved to `/api/v1/admin/auth/**` (Admin API) so Traefik can route on path prefix alone with no request inspection. Both APIs keep their own auth logic — consistent with "some services may be deployed together" (TRD §5). |
| 14 | Traefik discovery mechanism | Planned to use Traefik's Docker-labels provider; found while testing this stack that Docker Desktop on Windows doesn't reliably expose `/var/run/docker.sock` into a Linux container the way Traefik's docker provider expects (`Error response from daemon: ""` on every poll). | Switched to Traefik's static file provider (`infra/traefik/dynamic.yml`) with explicit routers/services pointing at Compose service DNS names. This is also a security improvement (Traefik needs no Docker API access at all) and works identically on Linux/Mac, so keeping it even outside this Windows quirk. |

| 15 | **YOLOv8 licence** | 5 migrated models are Ultralytics YOLOv8 weights, which are **AGPL-3.0**. AGPL's network-use clause means offering them in a hosted SaaS can oblige you to publish the source of the serving application, unless you hold an Ultralytics commercial licence. | Imported and recorded with `license: AGPL-3.0` and `review_required: true`, usable for development. **Needs a decision before commercial launch:** buy the Ultralytics commercial licence, or retrain/replace with permissively-licensed weights. This is a real cost/architecture input, not a formality. |
| 16 | **Biometric models** | 5 InsightFace models perform face detection, recognition (512-d embeddings), landmarking, and gender/age inference — used by the legacy `StaffAttendanceModel`. The PRD lists facial recognition as a release-one non-goal; the InsightFace buffalo_l terms are non-commercial; and face embeddings are special-category data under GDPR Art.9 and BIPA-style laws. | Per your instruction, **all five are migrated and preserved** — nothing was discarded. They are registered `state=revoked`, `access_classification=biometric`, so they cannot be attached to a pipeline until deliberately promoted, and the promotion API refuses a deployable state without `acknowledge_biometric=true`. Ready to wire into the control panel whenever you say; the gate exists so it's a decision someone makes on purpose, with an audit record. |
| 17 | Duplicate model filenames | The legacy tree had 22 model paths but only 14 distinct blobs, and **two different files were both named `yolov8n.pt`** (`31e20dde…` vs `f59b3d83…`). | Registry is content-addressed by SHA-256, so identity is the bytes, not the filename. The two are registered as separate models (`yolov8n-general`, `yolov8n-person`). Worth knowing which the legacy pipelines actually intended in each spot. |
| 18 | Legacy label maps | Only the fire/smoke model had an inferable label map; the rest carry none. | Recorded what could be established and flagged the rest. Accurate label maps are needed before any model is promoted to `production`, since incident types depend on them. |

## Model estate migrated from the legacy server

14 unique artifacts, 547 MB, all SHA-256 verified on transfer and stored content-addressed
in MinIO (`csense-models`). Legacy source paths and detection classes are recorded in each
version's `provenance`.

| Model | Task | State | Licence |
|---|---|---|---|
| `yolov8n-general` | object detection | validated | AGPL-3.0 |
| `yolov8n-person` | object detection (fall/crowd/zone) | validated | AGPL-3.0 |
| `yolov8n-pose` | pose estimation | validated | AGPL-3.0 |
| `yolov8m-pose` | pose estimation | validated | AGPL-3.0 |
| `fire-smoke-optimized150` | fire/smoke | validated | proprietary (unverified) |
| `fire-smoke-yolov8m` | fire/smoke | validated | AGPL-3.0 |
| `kitchen-safety-y8` | PPE / kitchen safety | validated | proprietary (unverified) |
| `license-plate-detector` | ANPR stage 1 | validated | verify upstream |
| `license-plate-ocr` | ANPR stage 2 | validated | verify upstream |
| `insightface-buffalo-l-detect` | face detection | **revoked** | non-commercial |
| `insightface-buffalo-l-recognition` | face recognition | **revoked** | non-commercial |
| `insightface-buffalo-l-landmark-3d` | face landmark | **revoked** | non-commercial |
| `insightface-buffalo-l-landmark-2d` | face landmark | **revoked** | non-commercial |
| `insightface-buffalo-l-genderage` | face attributes | **revoked** | non-commercial |

`validated` means "ran in production on the legacy platform" — **not** that it has passed
this platform's validation suite. Nothing is in `production` state yet; that gate is
Phase 4's golden-dataset and benchmark work.

If any default above is wrong, say so and I'll adjust — otherwise I'll keep building
against these assumptions and note anywhere they leak into a real constraint (e.g. license
terms on YOLOv8 for commercial use).
