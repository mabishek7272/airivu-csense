# 10 — Production Deployment Guide

**Version:** 1.0
**Status:** The mechanism is real and ready; the target environment is not chosen yet.

This guide gets AIRIVU CSense from "runs correctly on a developer's machine" to "runs
correctly on a real server with a real domain, real TLS, and real backups." Everything
in Part A is a genuine, human/business decision this document cannot make for you — no
amount of engineering substitutes for choosing a cloud provider or owning a domain. Part
B is the actual mechanism: real files, already in this repo, that use whatever Part A
decides.

---

## Part A — What only you can decide (do this first)

| Decision | Why it can't be pre-made | Where it plugs in |
|---|---|---|
| **A server or cloud account** | Business/budget choice — this repo doesn't assume AWS vs. a single VPS vs. on-prem hardware | Wherever `docker compose -f docker-compose.prod.yml` runs |
| **A real domain name(s)** | You have to own it | `infra/traefik/dynamic.prod.yml`'s `Host(...)` rules, `CUSTOMER_CRM_ORIGIN`/`DEVELOPER_CONSOLE_ORIGIN` |
| **DNS pointed at that server** | Requires the server to exist first | A/AAAA records for each domain, `:443` reachable |
| **An email address for Let's Encrypt notices** | Must be one you actually monitor | `ACME_EMAIL` |
| **Production secrets** (DB/Redis/MinIO passwords, JWT keypair, master encryption key) | Must be freshly generated for this deployment, never copied from `.env.example` or a dev environment | `infra/secrets/*`, `.env` |
| **A container registry** (optional but recommended) | Which one is a cost/tooling choice | `docker-compose.prod.yml`'s `build:` blocks — swap for `image: your-registry/csense-tenant-api:TAG` once a CI pipeline builds and pushes one; this repo's CI (`.github/workflows/ci.yml`) tests but does not currently publish images |
| **A log aggregator / monitoring target** | Datadog, Grafana Cloud, self-hosted Loki, CloudWatch — all valid, all different setup | Point your log shipper at each container's `json-file` output (capped, see the compose file), or swap the `logging:` driver |
| **A production SMTP/Resend account and WhatsApp Business credentials** | Real vendor accounts | `RESEND_API_KEY`, `WHATSAPP_GATEWAY_API_KEY`/`WHATSAPP_INSTANCE_TOKEN` |
| **A pilot tenant and go/no-go** | A real business decision, not a technical one (`CHECKLIST.md` Phase 9) | N/A — this is the human decision that starts real traffic |

Nothing below can substitute for these. What it *can* do is make sure that once you've
made them, there's a real, tested, correct path to a running production system — not a
"figure it out from the dev compose file" exercise.

---

## Part B — The mechanism

### B.1 Files involved

| File | Purpose |
|---|---|
| `infra/docker-compose.prod.yml` | Production compose definition — TLS via Traefik/Let's Encrypt, no host-published DB/cache/storage ports, `restart: unless-stopped` + resource limits + capped logging on every service. Standalone, not a merge-overlay on `docker-compose.yml` (Compose's list-merge semantics make partial overlays easy to get subtly wrong for exactly the things that matter here — see the file's own header comment). |
| `infra/traefik/dynamic.prod.yml` | The TLS counterpart to `dynamic.yml` — same routing/CORS/rate-limit shape, `websecure`/443 + a cert resolver on every router. **Contains `app.example.com`/`console.example.com`/`demo.example.com` placeholders you must replace.** |
| `.env` | Same file dev already uses, with production values |
| `infra/secrets/*` | JWT keypair + envelope-encryption master key — freshly generated for production, never reused from dev |

### B.2 Generate real secrets

**Never reuse the JWT keypair or master key from a dev/staging environment in
production.** Generate fresh ones:

```bash
cd infra/secrets
openssl genrsa -out jwt_private.pem 2048
openssl rsa -in jwt_private.pem -pubout -out jwt_public.pem
openssl rand -hex 32 > master_v1.key   # envelope-encryption master key
chmod 600 jwt_private.pem master_v1.key
```

Generate strong random passwords for `POSTGRES_PASSWORD`, `POSTGRES_API_PASSWORD`,
`POSTGRES_PLATFORM_API_PASSWORD`, `REDIS_PASSWORD`, `MINIO_ROOT_PASSWORD` — e.g.
`openssl rand -base64 32` per value. Put all of these, plus every value from Part A's
table, into a production `.env` (copy `.env`'s existing structure; do not commit it —
it's already gitignored).

### B.3 Replace the domain placeholders

```bash
cd infra/traefik
sed -i 's/app\.example\.com/app.yourdomain.com/g; s/console\.example\.com/console.yourdomain.com/g; s/demo\.example\.com/demo.yourdomain.com/g' dynamic.prod.yml
```

Set the matching `CUSTOMER_CRM_ORIGIN=https://app.yourdomain.com` and
`DEVELOPER_CONSOLE_ORIGIN=https://console.yourdomain.com` in `.env` — these two must
agree with `dynamic.prod.yml`'s `Host()` rules and CORS origin lists, or requests will
fail CORS in the browser even though routing itself works.

### B.4 Point DNS at your server

A/AAAA records for `app.yourdomain.com`, `console.yourdomain.com`,
`demo.yourdomain.com` (if using the demo site) → your server's public IP. Let's Encrypt's
HTTP-01 challenge (what `docker-compose.prod.yml` is configured for) needs port 80
reachable from the public internet at the moment certificates are issued/renewed — don't
firewall it off.

### B.5 Bring it up

```bash
cd infra
docker compose -f docker-compose.prod.yml --env-file ../.env build
docker compose -f docker-compose.prod.yml --env-file ../.env run --rm migrate
docker compose -f docker-compose.prod.yml --env-file ../.env up -d
docker compose -f docker-compose.prod.yml --env-file ../.env ps
```

Watch the `traefik` logs for the first certificate issuance
(`docker compose -f docker-compose.prod.yml logs -f traefik`) — a failure here is almost
always DNS not yet propagated or port 80 not reachable, not a Traefik config problem.

### B.6 Bootstrap the first platform admin

There is no self-service path to platform scope, by design (Operations Manual §A.2) —
insert the first `platform_admin` directly, then enroll their MFA immediately.

### B.7 Verify for real before declaring it live

Don't take "the containers are running" as proof it works — run a real smoke test the
same way this whole platform was built and verified:

```bash
cd scripts
python e2e_vertical_slice.py   # a broad, real path through register -> camera -> incident
python backup.py               # confirm backups work against the real production DB from day one
python restore_exercise.py     # confirm restores work before you ever need them for real
```

Point these at your production host by adjusting the hardcoded `localhost:8080` base URL
in each script, or run them from a machine on the same network as the server with that
substitution — they were written against `localhost` because every verification run this
whole build went through was local; production verification is the same scripts, a
different target.

### B.8 Go-live checklist

- [ ] Fresh secrets generated (B.2), never copied from dev
- [ ] Domain placeholders replaced in `dynamic.prod.yml` (B.3), origins match in `.env`
- [ ] DNS propagated, port 80/443 reachable (B.4)
- [ ] TLS certificate actually issued (check `docker compose logs traefik`, or just visit
      `https://app.yourdomain.com` and check the padlock)
- [ ] Migrations applied (`migrate` service exited 0)
- [ ] First platform admin bootstrapped, MFA enrolled (B.6)
- [ ] `backup.py` run once successfully against the real production database
- [ ] `restore_exercise.py` run once successfully — proves the backup mechanism works
      *before* you need it, not after
- [ ] A real smoke test passed against the production URL (B.7)
- [ ] Log shipping configured to wherever you monitor (Part A) — don't rely on
      `docker logs` retention alone
- [ ] A real pilot tenant and cutover window agreed (`CHECKLIST.md` Phase 9 — this is
      the one item on this whole list that is a business decision, not a technical one)

### B.9 Rollback

If something is badly wrong immediately after a deploy:

```bash
docker compose -f docker-compose.prod.yml --env-file ../.env down
# fix forward, or restore the previous known-good image/config and:
docker compose -f docker-compose.prod.yml --env-file ../.env up -d
```

If the problem involves bad data (not just bad code), see
`docs/09_INCIDENT_RUNBOOKS.md` RB-10 — restore from the last verified backup rather than
attempting to hand-patch production data under pressure.

### B.10 What this deployment does *not* give you yet

Named honestly, per this project's own standing discipline of not claiming more than was
actually built:

- **Multi-node/Kubernetes deployment.** `docs/00_DOCUMENT_INDEX.md`'s own architecture
  baseline is explicit: Docker Compose is the target for pilot/smaller deployments;
  Kubernetes is introduced only once multi-node scale or availability targets justify
  it. This guide is the Compose path.
- **Managed/clustered Postgres, Redis, or object storage.** The compose file runs
  single-instance containers with a local volume for each. For real production
  durability, point `POSTGRES_HOST`/`REDIS_HOST`/`MINIO_ENDPOINT` at managed equivalents
  (RDS, ElastiCache, S3-compatible storage, or your cloud's own equivalents) instead —
  the application code already only knows these as hostnames/credentials from `.env`, so
  this swap needs no code change, only infrastructure and config.
- **Real off-site backup replication.** `scripts/backup.py` proves the backup mechanism
  and writes into this deployment's own MinIO — a real disaster-recovery posture needs
  those backups replicated to storage that survives losing this entire host, which needs
  a second, genuinely separate target you choose.
- **GPU-backed inference, an edge agent, or WireGuard/relay-based camera connectivity at
  scale.** All named explicitly as external-input-blocked in `CHECKLIST.md` — real
  hardware and real business decisions this document can't manufacture.
- **An independent security audit / penetration test.** Automated scanning
  (`docs/07_OPERATIONS_MANUAL.md` §B.7) is real and running; it is not a substitute for
  one, and `CHECKLIST.md` names this as needing a contracted third party.
