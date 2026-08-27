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

| 15 | YOLOv8 licence | 5 migrated models are Ultralytics YOLOv8 weights under **AGPL-3.0**. | **DECIDED 2026-08-26 (owner):** these models have been in production use since 2022; the owner accepts the position and directed that all be wired up. Licence terms stay recorded on each version (`license_metadata`) so the facts remain visible if the position is ever revisited. No further action pending. |
| 16 | Biometric models | 5 InsightFace models (face detection, 512-d recognition embeddings, landmarks, gender/age) behind the legacy `StaffAttendanceModel`. | **DECIDED 2026-08-26 (owner):** in production use since 2022; owner directed all models be wired up. Promoted to `production` with `access_classification=biometric` retained and every transition audited. The classification stays as metadata rather than a block, so operators can still see what these are and a future privacy review can find them in one query. |
| 17 | Duplicate model filenames | The legacy tree had 22 model paths but only 14 distinct blobs, and **two different files were both named `yolov8n.pt`** (`31e20dde…` vs `f59b3d83…`). | Registry is content-addressed by SHA-256, so identity is the bytes, not the filename. The two are registered as separate models (`yolov8n-general`, `yolov8n-person`). Worth knowing which the legacy pipelines actually intended in each spot. |
| 18 | Legacy label maps | Only the fire/smoke model had an inferable label map; the rest carry none. | Recorded what could be established and flagged the rest. Accurate label maps are needed before any model is promoted to `production`, since incident types depend on them. |
| 19 | **Detections: PostgreSQL, not MongoDB** | docs/05_BACKEND_SCHEMA.md §2 assigns high-volume detections to MongoDB. | **CHANGED 2026-08-26 at owner's direction.** Detections now live in PostgreSQL (migration 0012) and MongoDB is removed from the stack. This is a Class C change under the spec's own governance (§14) and is the better design here: (a) detection tenant isolation moves from an application-level filter to database-enforced RLS — a security improvement, since the old wrapper was one forgotten call from a cross-tenant read; (b) detections and incidents now commit in one transaction with a real foreign key, where before a detection could be written and its incident lost; (c) one datastore to back up, restore, patch and monitor instead of two. The tradeoff is write throughput at extreme scale — the design inputs are 200/sec sustained (~17M rows/day), which one PostgreSQL node handles. **Revisit trigger:** sustained >10M rows/month or degrading p95 on camera-history queries → convert `detections` to monthly RANGE partitions on `capture_time`. Deliberately not partitioned now, because partitioning would force the idempotency key to include `capture_time` and weaken the guarantee that stops spool replay creating duplicates. |
| 20 | Telemetry collections | SCH §12 also assigns `edge_telemetry`, `camera_telemetry`, `ai_runtime_metrics` and `technical_events` to MongoDB. None are built yet. | They will follow detections into PostgreSQL when built, for the same reasons. Flagging so nobody reintroduces MongoDB for one of them by default. |
| 21 | **Evolution Go licence** | Vendored for WhatsApp. Apache 2.0 **plus two extra conditions**, and a separate TRADEMARKS policy. | Rebranding is not just permitted, it is **required**: TRADEMARKS §4.2 says a modified UI must remove their marks and take a clearly distinct name. Two obligations we must meet: (a) licence §1.b requires a visible notice to system administrators that Evolution Go is in use — implemented as an "Open source components" panel in the Developer Console; (b) Apache §4(d) requires preserving NOTICE. We run it **headless (API only)** and do not ship their `manager/dist` console, which sidesteps §1.a entirely (that clause applies only when using their frontend components) and means we are not distributing a modified version of their UI. |
| 22 | **WhatsApp ban risk** | `whatsmeow` is an unofficial WhatsApp Web client. Meta's terms prohibit unofficial clients; numbers are banned without warning. For safety alerting that means notifications stop silently. | **DECIDED 2026-08-26 (owner):** ship Evolution Go now, behind a provider interface, so Meta's official WhatsApp Business Cloud API can replace it later without touching the rest of the system. Delivery failures are recorded per-attempt, so a ban shows up as a failure rate rather than silence. |
| 23 | Evolution Go telemetry | Sends `{route, apiVersion, timestamp}` to `log.evolution-api.com` on **every** API request. No message content or customer data. Not disableable by config. | Apache 2.0 permits modification, so the telemetry middleware is removed in our build. Worth knowing regardless: it is an outbound call per request, which matters for on-prem or air-gapped deployments. Their licence auto-activation also calls `evolutionapi.com` at startup. |
| 24 | Email: Resend + Cloudflare | **DECIDED 2026-08-26 (owner):** Resend for outbound, Cloudflare for DNS **and** Email Routing for inbound. | Outbound via Resend API. Cloudflare hosts SPF/DKIM/DMARC for the sending domain. Inbound replies route through Cloudflare Email Routing to a webhook, enabling reply-to-acknowledge on an alert. **Needs from you:** a Resend API key, the sending domain, and DNS access — see the setup section in docs. |
| 25 | **Evolution Go requires vendor licence activation** | Discovered while building: the gateway returns **503 `LICENSE_REQUIRED` on every API endpoint** until a licence is registered with Evolution Foundation. Their intended activation flow goes through the manager console we deliberately do not ship. | **NEEDS YOUR ACTION.** Not something to work around — it is the vendor's licensing gate. Two legitimate routes: (a) open the registration URL surfaced at `GET /api/v1/admin/notifications/whatsapp/licence` and complete it in a browser; or (b) set `EVOLUTION_OPERATOR_EMAIL` on the `whatsapp-gateway` service to an address already registered with them, for headless auto-activation. **WhatsApp alerting cannot send until this is done.** Email is unaffected. The console surfaces the state and the URL so this shows as a setup step rather than an unexplained outage. |
| 26 | Resend credentials | ~~Needs an API key and verified sending domain.~~ **DONE 2026-08-26.** | Key configured; sending as `CSense Alerts <no-reply@3rdi.in>` with `support@3rdi.in` as reply-to. **Verified with a real send** — Resend accepted message `2924ba5d-…` to `3rdi.csense@gmail.com`. Cloudflare Email Routing already forwards `no-reply@`, `support@` and `hello@3rdi.in`. The inbound webhook that turns a reply into an acknowledgement is still to build. |
| 27 | **Gateway API contract corrected** | My first WhatsApp provider was written against a guessed contract. Reading the vendored routes showed it wrong in three ways. | Fixed: (a) the gateway has **two** auth modes — the global key authorises instance create/list/delete, while status/QR/send/logout authenticate with the **instance's own token**, which is how the gateway identifies the number; (b) `create` requires `{name, token}` and the token is supplied by us, so it lives in config rather than being captured from a response; (c) `logout` is DELETE, not POST. Worth noting as a general lesson: the endpoint names looked obvious enough that I did not check first, and every one of those three assumptions was wrong. |
| 28 | Cloudflare R2 not reachable | `<account>.r2.cloudflarestorage.com` resolves to Cloudflare but the TLS handshake returns "no peer certificate available", from this machine and from inside a container. A control test against `*.r2.dev` presents a valid cert, so it is not local. Cloudflare only serves TLS on that hostname for accounts with R2 enabled. | **NO LONGER BLOCKING — see #29.** R2 turned out not to be needed at all. Credentials remain in `.env` if you later want R2 for backups or exports, but nothing depends on it. If you do enable it, the account id is `6754bbb4…`. |
| 29 | ~~Alert media needs public storage~~ **WRONG — corrected 2026-08-26.** | I claimed snapshots in alerts required publicly reachable storage, and pushed R2 as the fix. That was asserted without checking, and it was wrong. | Neither channel needs a public URL. **WhatsApp:** the gateway calls `http.Get(url)` and downloads the media *itself* before uploading the bytes to WhatsApp — and it runs inside our Docker network, so an internal MinIO URL is fine; WhatsApp never sees our URL. **Email:** snapshots are now **attached** rather than hotlinked, referenced by `cid:`. That sidesteps Gmail's image proxy entirely and has a real advantage — an attached snapshot still opens months later, when any presigned URL would long since have expired. Verified with a real send carrying a 58 KB annotated snapshot. |

## Model estate migrated from the legacy server

14 unique artifacts, 547 MB, all SHA-256 verified on transfer and stored content-addressed
in MinIO (`csense-models`). Legacy source paths and detection classes are recorded in each
version's `provenance`.

| Model | Task | State | Licence |
|---|---|---|---|
| `yolov8n-general` | object detection | production | AGPL-3.0 |
| `yolov8n-person` | object detection (fall/crowd/zone) | production | AGPL-3.0 |
| `yolov8n-pose` | pose estimation | production | AGPL-3.0 |
| `yolov8m-pose` | pose estimation | production | AGPL-3.0 |
| `fire-smoke-optimized150` | fire/smoke | production | proprietary (unverified) |
| `fire-smoke-yolov8m` | fire/smoke | production | AGPL-3.0 |
| `kitchen-safety-y8` | PPE / kitchen safety | production | proprietary (unverified) |
| `license-plate-detector` | ANPR stage 1 | production | verify upstream |
| `license-plate-ocr` | ANPR stage 2 | production | verify upstream |
| `insightface-buffalo-l-detect` | face detection | production | non-commercial |
| `insightface-buffalo-l-recognition` | face recognition | production | non-commercial |
| `insightface-buffalo-l-landmark-3d` | face landmark | production | non-commercial |
| `insightface-buffalo-l-landmark-2d` | face landmark | production | non-commercial |
| `insightface-buffalo-l-genderage` | face attributes | production | non-commercial |

All 14 were promoted to `production` on 2026-08-26 by owner direction, having been in
continuous legacy service since 2022. Worth being precise about what that means: it
reflects *legacy* production-proven status, not a pass through this platform's own
golden-dataset validation suite. That harness is still outstanding Phase 4 work, and
until it exists `model_validation_runs` stays empty for these versions.

If any default above is wrong, say so and I'll adjust — otherwise I'll keep building
against these assumptions and note anywhere they leak into a real constraint (e.g. license
terms on YOLOv8 for commercial use).

---

## 30. WhatsApp gateway leaked a Postgres connection pool per reconnect (fixed)

**Symptom:** `/instance/qr` returned `400 no QR code available`, indefinitely. It looked
like a broken QR endpoint.

**Cause:** the vendored gateway called `sqlstore.New()` inline on every connection
attempt. That opens a `*sql.DB`, which is a connection *pool* with no default limit, and
nothing closed the discarded ones. The gateway retries roughly every fifteen seconds while
a QR goes unscanned, so it climbed to ~90 of the server's 100 connection slots over a few
hours - at which point it could no longer open its own session store, and every other
service was refused connections too. `psql` itself could not connect.

**Fix:** one store per process, created on first use behind a mutex
(`pkg/whatsmeow/service/whatsmeow.go`, `sessionStore`). It is a package-level singleton
rather than a struct field because `StartClient` takes a *value* receiver - anything
cached on the struct is written to a copy and discarded, and an embedded mutex would be
copied with it. Verified: connections held steady at 10-11 across repeated connects,
where previously they grew without bound.

**Worth carrying forward:** the leak was invisible in the gateway's own logs, which only
reported the downstream `too many clients` error. Connection-count monitoring on Postgres
would have named this in minutes rather than hours.

## 31. Two QR rendering attempts produced images that scanned as nothing

Both tried to recover the QR module grid by sampling the gateway's rendered 256px PNG.
That cannot work reliably: at roughly three pixels per module the sampling is ambiguous,
and a grid wrong by a single module still *looks* exactly like a QR code while decoding as
nothing - which is a slow and demoralising thing to discover while holding a phone.

The gateway's `/instance/qr` response carries a `code` field alongside the PNG: the raw
payload string. Encoding that directly removes the guesswork, and `--png` now verifies its
own output with a decoder before claiming it is scannable.

One correction on the record: I described the gateway's own PNG as unscannable because
OpenCV would not decode it. A phone camera read it fine, and that is how the number was
eventually linked. OpenCV's decoder is stricter than a phone's; "cv2 cannot read it" is
not the same claim as "it will not scan".
