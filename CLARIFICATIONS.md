# Open Clarifications

Things that materially affect scope or correctness and need a human decision. None of
these block foundational engineering work, so I'm proceeding with the stated defaults and
will course-correct when answered.

| # | Topic | Question | Default I'm proceeding with |
|---|---|---|---|
| 1 | Legacy system | Does a real legacy CSense codebase/database exist to migrate, or is this fully greenfield? | Greenfield. Migration tooling (Phase 7) will be built generically but unvalidated until real source data exists. |
| 2 | Team/timeline | The source plan assumes ~20 people over 44 weeks. Actual execution here is solo/AI-driven. | Re-scoped checklist in [CHECKLIST.md](CHECKLIST.md); features build in the same phase order but without hardware-lab/pentest/production-rollout items that need people or contracts I can't supply. |
| 3 | Deployment target | Cloud provider / on-prem / region? | **Local Docker Compose** (confirmed by user). Kubernetes/cloud IaC deferred until a real target environment is chosen. |
| 4 | Pilot tenant & use cases | Who is the first real customer and which detection use cases matter most (intrusion, PPE, ANPR, fire/smoke, etc.)? | Building generic pipeline framework; reference pipeline will target a common case (restricted-zone intrusion) as the first working example. |
| 5 | Camera/NVR hardware | No physical cameras/NVRs available. | Will validate against RTSP test streams / simulated camera sources instead of a certified hardware matrix. |
| 6 | AI models | Build in-house, license, or use open-source? | Defaulting to open-source YOLOv8 (Ultralytics, AGPL/enterprise-license caveat — flagging for legal review before any commercial use) exported to ONNX, run via ONNX Runtime. |
| 7 | Notification providers | No SMS/push/email provider contracted (Twilio, SendGrid, FCM, etc.). | Building a pluggable provider-adapter interface with a working email adapter (SMTP) and a console/log adapter for SMS/push so the pipeline is real and swappable once a provider is chosen. |
| 8 | Legal/privacy jurisdiction | CCTV and data-retention law varies by country; not specified. | Defaulting to conservative settings: masking-on-by-default, explicit retention policy fields, no default facial recognition — but **do not treat this as legal compliance advice**; a real deployment needs jurisdiction-specific legal review. |
| 9 | Billing system | External billing system referenced but unnamed. | Out of scope for release one per the PRD itself; building the entitlement/quota model so it can plug into a billing system later. |
| 10 | Git remote / CI runner | No GitHub/GitLab remote configured. | CI workflow files are written but won't actually run until a remote is added — tell me if/where to push. |
| 11a | Platform role assignment | docs/05_BACKEND_SCHEMA.md §5.9 defines `platform_developers` but not how a developer gets a platform-audience role (memberships are tenant-scoped and can't hold a NULL tenant_id). | Added a small `platform_role_assignments` table (developer ↔ role) as a spec-consistent extension — flagging for architecture review rather than treating it as silently decided. |
| 11 | Secrets management | No secret manager (Vault/AWS Secrets Manager/etc.) specified for local dev. | Using `.env` files (git-ignored) + documented rotation path for local dev; production secret store is a Phase 8/9 decision tied to #3. |

| 12 | Local openssl quirk | This machine's Git Bash `openssl` (mingw64 build) fails silently writing to a path containing an apostrophe (`kamal's loq`) when given an MSYS-style `/c/...` path. | Fixed in `scripts/generate_dev_secrets.sh` by converting to a Windows-style path via `cygpath -m` before calling openssl. Verified working. Worth knowing if you write other scripts that shell out to openssl/native Windows tools from this repo's path. |

| 13 | Auth route split | TRD §10.2 shows one illustrative `/api/v1/auth/**` path but also says (§6.3) gateway routing is by path prefix per API, and doesn't run a separate standalone Identity Service. | Customer auth stays at `/api/v1/auth/**` (Tenant API); platform auth moved to `/api/v1/admin/auth/**` (Admin API) so Traefik can route on path prefix alone with no request inspection. Both APIs keep their own auth logic — consistent with "some services may be deployed together" (TRD §5). |
| 14 | Traefik discovery mechanism | Planned to use Traefik's Docker-labels provider; found while testing this stack that Docker Desktop on Windows doesn't reliably expose `/var/run/docker.sock` into a Linux container the way Traefik's docker provider expects (`Error response from daemon: ""` on every poll). | Switched to Traefik's static file provider (`infra/traefik/dynamic.yml`) with explicit routers/services pointing at Compose service DNS names. This is also a security improvement (Traefik needs no Docker API access at all) and works identically on Linux/Mac, so keeping it even outside this Windows quirk. |

If any default above is wrong, say so and I'll adjust — otherwise I'll keep building
against these assumptions and note anywhere they leak into a real constraint (e.g. license
terms on YOLOv8 for commercial use).
