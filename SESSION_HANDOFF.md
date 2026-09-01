# Session handoff — AIRIVU CSense

**Point-in-time snapshot, written 2026-09-01.** Paste this as your first message in a new
Claude Code session (e.g. after cloning this repo onto a new machine) to pick up where the
previous session left off. `CLAUDE.md` and `CHECKLIST.md` are read automatically and cover
the durable "what's built and why" — those stay current; this file is a one-time snapshot
of the narrative and exact state as of the date above, and will drift out of date as work
continues. If you're reading this well after that date, trust `CHECKLIST.md`'s state over
this file's "current exact state" section, and feel free to delete this file once its
job's done.

---

I'm continuing work on AIRIVU CSense (this repo) on branch `phase-1-foundation`, under a
standing directive: complete the CHECKLIST.md items autonomously, verify everything for
real against the live Docker stack (not just read-through), document decisions in code
comments / CHECKLIST sub-bullets rather than asking, and only pause to ask when a decision
materially changes direction or effort. Commit as "Sara" with trailer
`Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` (or your own current model name),
push to `https://github.com/mabishek7272/airivu-csense.git`.

**Read `CLAUDE.md` first** — it has non-obvious project facts (production server specs,
the reference NVR's H.265-only constraint, the WireGuard connectivity model, the
quiet-hours design) that shaped real decisions already in the code. Then skim
`CHECKLIST.md` for what's done, partial (`[~]`), or blocked (`[!]`).

## What happened most recently (this machine, this session)

1. **Mobile app pivot**: built a native Android app (Kotlin/Jetpack Compose) first, then
   the user explicitly redirected both platforms to one shared Expo/React Native
   codebase (`mobile/app/`) once it turned out Expo tooling was already available. The
   native app was deleted (preserved in git history); Expo app shipped with the same
   backend-contract knowledge carried forward. 17 Jest tests, CI green (`mobile-app`
   job).
2. **Async incident exports**: `POST /api/v1/tenant/exports/incidents` → background CSV
   generation → time-limited presigned MinIO download. Real e2e-verified against the
   live stack. CI green.
3. **This dev machine (Windows, 12GB RAM) has had Docker Desktop crash outright twice**
   this session (not just slow — the WSL2 distro stopped, all Docker processes exited),
   root-caused earlier in the session to real RAM pressure (measured as low as 0.66GB
   free at one point). Recovered both times via kill + relaunch, but it's a recurring
   problem on this hardware.
4. **Decision made**: the user is switching primary dev machine to a Mac M4 Pro
   (36GB RAM, 10-core) — recommended given the repeated, measured RAM-pressure crashes
   here, and the user already needs that Mac for real iOS builds anyway (Expo/EAS). This
   file, plus `CLAUDE.md`, is the result of making sure that switch doesn't lose
   anything — everything durable is now in the repo, not just session memory.
5. **Declined**: connecting to a third-party "Hyperspace" P2P/AGI compute-sharing network
   on the new machine — unrelated to CSense, and it wants to run as a residential-IP
   proxy + always-on daemon, which is a real security/legal exposure on a machine that
   will hold production secrets and camera credentials. Not done, not planned.

## Current exact state

- Branch: `phase-1-foundation` (not yet merged to `main`)
- Latest commits (newest first): `CLAUDE.md` added → exports CI ruff fix → async
  incident exports feature → mobile app pivot (Expo/React Native)
- CI green on all jobs (backend, both frontends, mobile-app, security scans) as of the
  last push
- No open blockers on `phase-1-foundation`; nothing uncommitted was left behind on the
  old machine (verified via `git status` before this handoff was written)

## Immediate next steps (per the standing checklist)

From `CHECKLIST.md`'s Phase 6, still open and not blocked on external input:
- Edge encrypted offline spool, reconnect cursor, batch resync, dedup
- WireGuard/relay integration design + time-limited diagnostic access (ties into the
  already-partial support-grants feature — see `CHECKLIST.md`'s `[~]` entry on it)
- The deferred halves already named in `CHECKLIST.md`: support-grant *authorization*
  (actually elevating a token's reach, not just the request/approve workflow), automatic
  webhook delivery driven by the outbox

Everything else not-yet-done in `CHECKLIST.md` is genuinely blocked on human/business
input (a named pilot tenant, a cloud account, a contracted SMS/push provider, a legacy
system to migrate from) — don't attempt those, just keep them flagged.

## Practical setup on a new machine

```bash
git clone https://github.com/mabishek7272/airivu-csense.git
cd airivu-csense
cp .env.example .env   # if present - check infra/ for what's actually needed
cd infra && docker compose --env-file ../.env up -d --build
```
Then confirm `docker compose ps` shows everything healthy, and optionally run one of the
`scripts/e2e_*.py` scripts against the live stack to confirm the environment is wired up
correctly before continuing feature work.
