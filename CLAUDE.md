# AIRIVU CSense — project notes for Claude

This file exists so non-obvious, load-bearing facts survive a machine switch or a new
session — they shaped real design decisions already in the code, and aren't otherwise
written down in `docs/` or discoverable by reading the code alone. If you're picking this
project up fresh: read `CHECKLIST.md` first for what's built and why; read this file for
the facts behind a few specific decisions you'll otherwise have to re-derive.

## Production server: 16 cores / 32GB RAM / 600GB disk, no GPU

Stated by the customer 2026-08-27. This is the actual deployment target `infra/docker-
compose.prod.yml` is built for — everything video-related is CPU-bound, no NVENC/QSV/CUDA
offload available.

Camera capacity, measured (≈13 usable cores after ~3 reserved for platform services — the
idle stack itself measures ~0.05 cores / 1GB RAM, so the platform isn't the constraint):

| Inference source | fps | Cores/camera | Cameras (13 cores) |
|---|---|---|---|
| Mainstream, keyframes only | 0.5 | 0.08 | ~160 |
| Substream 704x576, full decode | 2 | 0.25 | ~50 |
| Mainstream, full decode | 2 | 0.72 | ~18 |

Live view adds ~0.23 cores per **concurrent viewer** (substream transcode to H.264 for
WebRTC), not per camera — it's on-demand, size by expected simultaneous watchers, not
camera count.

**Continuous recording does not fit this box.** Mainstream is ~4.1 Mbps = 1.85 GB/hour/
camera; 30 days of one camera alone is ~1.3 TB against 600 GB total. Evidence snapshots
(~1.5 MB/incident, all 3 variants) or short event clips (~5 MB/10s) are what fit instead —
roughly 330k incidents or 100k clips in 500 GB usable.

**Highest-value lever if the customer will change it**: the reference NVR's GOP is
currently 40 frames (2s) → 0.5 fps from keyframe-only sampling. Shortening it to 1s GOP
gives 1 fps at full mainstream resolution for ~0.06 cores/camera — the best quality-per-
core available without a GPU.

## The reference NVR streams H.265/HEVC only — no H.264 fallback

`autotek-dorani-nvr.dyndns.org:554` (ONVIF, reachable over public DynDNS). Verified by
direct RTSP DESCRIBE and frame decoding on every channel/substream tested (c1/c3, s0/s1).
Digest auth; **TCP transport required** — UDP over the public internet smears frames.
Night imagery is IR greyscale, wide-angle/fisheye.

**This is why WebRTC live view needs a transcode step and HLS doesn't.** Confirmed
empirically: Chromium's `RTCRtpReceiver.getCapabilities('video')` offers VP8/VP9/H264/AV1
— no H.265 — and MediaMTX's WebRTC endpoint 400s on the H.265 path. HLS serves the H.265
stream natively (`CODECS="hvc1.1.2.L123.80"`).

Transcoding cost, measured on 1 CPU core (AMD Ryzen 5 7235HS, libx264 veryfast) against a
recorded sample (a live 20fps source caps throughput at 1.0x and can't be measured past
that):

| Work | Throughput | Cores/camera |
|---|---|---|
| Decode only, 2592x1520 H.265 | 2.02x | 0.5 |
| Transcode → 2592x1520 H.264 | 0.60x | **1.7** |
| Transcode → 1280x720 H.264 | 0.89x | 1.1 |
| Transcode 704x576 substream → H.264 | 4.42x | **0.23** |

**Design conclusion baked into the code**: transcode the **substream** for live view, not
the mainstream — 0.23 cores/camera vs 1.7 (a 20-camera site is ~4.5 cores instead of ~34).
Hardware encoding would change these numbers substantially and is untested (the prod box
has no GPU anyway, per above).

**Night/IR detection quality is poor and needs its own thresholds.**
`yolov8n-general` on the night IR mainstream found monitors/a person only at 0.09–0.21
confidence — a production `min_confidence` of 0.5 would detect nothing at night on this
camera. The substream was worse (missed monitors entirely). Treat night detection
thresholds as unvalidated until there's a real night validation set.

Credentials for this NVR were shared in chat during testing and should be rotated before
any real reliance on this device continues.

## Connectivity model: cameras reach the platform four different ways

(RTSP Mentor Guide ch. 3-7). The address *type* differs per method, which is why the SSRF
allowlist (`csense_shared.security.outbound`) needs an allowlist rather than a blanket
private-address refusal:

| Method | Address CSense connects to |
|---|---|
| **WireGuard VPN** (recommended for production) | `10.0.0.x` — private, via tunnel |
| **NVR middleman + VPN** | peer address, channels like `/Streaming/Channels/101` |
| **Cloud relay** | relay's public IP |
| **Port forward + DDNS** | public DDNS name — what the reference Autotek NVR uses |

Modeled as `cameras.connection_mode` (`direct`/`vpn`/`edge`/`cloud_relay`); tunnel modes
require an `edge_device_id`. The allowlist is built from that device's `vpn_address` (/32)
plus its `lan_cidr`.

**WireGuard provisioning is fully app-driven** (`POST /devices/{id}/vpn-provision` in
`backend/tenant_api/app/api/edge.py`) — nobody hand-edits a deployment-guide template.
Three real flaws were found and closed while building this (2026-08-29), worth knowing if
you're touching this code:
1. A rendered peer's `AllowedIPs` is scoped to exactly that device's own `/32` + its own
   `lan_cidr` — the original guide's `10.0.0.0/24` on every client let one tenant's device
   route to another tenant's cameras.
2. `vpn_pool.py` allocates a fleet-wide-unique `/32` from `10.8.0.0/16` — the guide's
   hardcoded `10.0.0.2` collided across sites.
3. An application-level overlap check inside `vpn-provision` (under a table-level `LOCK`)
   refuses two tenants declaring overlapping `lan_cidr` (e.g. both using
   `192.168.1.0/24`) on the shared WireGuard server — this would otherwise let a second
   peer silently steal routing from the first. The check needs cross-tenant visibility RLS
   normally forbids; `edge_vpn_pool_snapshot()` (migration 0029) is a narrow
   `SECURITY DEFINER` read built for exactly this (addresses/ranges only, never which
   tenant owns them).

**Non-obvious trap**: `172.18.0.0/16` looks like an ordinary tenant site LAN but is this
deployment's own Docker bridge network — declaring it as a site CIDR would route traffic
to the host's local network instead of the tunnel, reaching this deployment's own
Postgres. `RESERVED_LOCAL_NETWORKS` config exists specifically to catch this; it's
deployment-specific (currently the Docker bridge ranges for local dev) and must be set
correctly per real deployment, not left at whatever default ships.

## Quiet hours: per-recipient, not per-policy or per-step

Two design decisions made 2026-08-29 without an explicit spec — worth knowing if this
needs revisiting:

**Quiet hours live on `recipient_group_members.active_schedule`** (one person's own
schedule) — not on a notification policy or an escalation step. Migration 0015's schema
comment already implied this ("A fire alarm ignores them; a housekeeping alert should
not") but nothing had wired it up before. Two people in the same escalation step can
therefore be reached at different hours — deliberate; a policy- or step-level setting
couldn't express that.

**`high`/`critical` severities always bypass a recipient's quiet hours; `info`/`low`/
`medium` are held until the window ends.** This exact cutoff
(`QUIET_HOURS_EXEMPT_SEVERITIES` in `csense_shared/notifications/dispatcher.py`) isn't
specified anywhere in the docs — migration 0015 gives the shape of the rule but not the
precise severity threshold. Chosen to match how the rest of the platform already treats
these two severities as "something is actively wrong, respond now." If the intended
cutoff turns out to be different (e.g. only `critical` should bypass), it's a one-line
change — but every assertion in `scripts/e2e_notification_config.py` would need updating
alongside it.

A held delivery is delayed until the window ends (`quiet_hours_end` in `schedule.py`),
never dropped — consistent with the platform's general "late alert beats no alert" stance
(`MAX_DELAY_SECONDS`, the escalation ladder design).

## Night/IR detection: Confidence thresholds and tuning

The pipeline's detection models (especially `yolov8n-general`) produce significantly lower
confidence scores under night/IR lighting than under daylight. This is **not a bug** — it is
expected behavior for neural networks under poor lighting. However, it requires operators
to understand and tune thresholds accordingly.

**Measured on the reference camera (Autotek Dorani NVR, IR greyscale night footage):**
- `yolov8n-general` (object detection): 0.09–0.21 confidence for valid detections (monitors, people)
- `yolov8n-person`: unvalidated; likely similar range
- Substream (704x576): worse confidence than mainstream; skipped from night processing

**Default pipeline behavior:**
The pipeline runtime uses `camera_assignment.min_confidence` (per-rule, defaults to 0.5) as
a filter. A detection with 0.15 confidence against a 0.5 threshold is **silently discarded**
at filter time — the frame is marked "clean" even though valid objects were detected.

**How operators should tune:**
1. Create a separate rule for night cameras with `min_confidence: 0.15` (or lower, up to 0.09)
2. Test against real night footage from that camera (not assumptions)
3. Monitor false-positive rate; adjust threshold up if too noisy, down if missing detections
4. Do NOT set a single global threshold for all cameras — day/night split is essential

**Schema reference:**
- `pipelines.rules[].min_confidence: float` (defaults to 0.5, no lower bound enforced at schema level)
- Per-camera rule matching: `backend/shared/csense_shared/pipeline/rules.py`, line 17

**Example rule (via API):**
```json
POST /api/v1/tenant/rules
{
  "name": "night_perimeter_intrusion",
  "model_name": "yolov8n-general",
  "site_id": "...",
  "camera_ids": ["night_cam_id"],
  "min_confidence": 0.15,
  "classes": ["person"],
  "zone_ids": ["perimeter_zone"]
}
```

**Why this design:**
- Thresholds are **per-rule**, not per-camera or per-model, because the same camera may
  have different confidence requirements for different use cases (intrusion detection can
  tolerate lower confidence; PPE or face detection cannot).
- No automatic night-mode detection exists because "night" is not a camera property; it's
  a time-of-day property that varies by geography/season/weather. The operator knows their
  deployment best.

**Known limitation:** These thresholds are **unvalidated by this build's own harness** —
they are based on spot measurement against one camera's night footage, not a golden dataset.
A future Phase 4 validation pass will re-tune these against a real night validation set
if one becomes available.

## Working conventions this session established

- **Verify for real, not by inspection.** Every feature in `CHECKLIST.md` marked `[x]`
  was run against the live Docker stack (`docker compose up`, real migrations, a real
  `scripts/e2e_*.py` script hitting real HTTP endpoints) before being marked done — not
  just unit-tested or read through. `[~]` means partially done with the gap named
  explicitly; `[!]` means blocked on something only a human can supply (a real pilot
  tenant, a cloud account, a contracted vendor). Keep this discipline — a checklist item
  claiming completion should mean what it says.
- **Decisions get documented, not just made.** Where a choice had no explicit spec (the
  quiet-hours cutoff above is one example), the reasoning is written into a code comment
  or a `CHECKLIST.md` sub-bullet at the point of the decision, so it can be revisited
  deliberately later instead of rediscovered by accident.
- **Commit as "Sara"**, trailer `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` (or
  the current model's own name/version if that's changed), pushing to
  `https://github.com/mabishek7272/airivu-csense.git`. Branch first off `main` if working
  on the default branch; don't amend existing commits or force-push without being asked.
