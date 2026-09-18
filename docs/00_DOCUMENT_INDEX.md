# AIRIVU CSense Modernization Specification Pack

**Version:** 1.0  
**Status:** Implementation baseline  
**Date:** 19 August 2026  
**Product:** AIRIVU CSense / 3RDi  
**Classification:** Proprietary and confidential

## Purpose

This pack converts the approved CSense modernization direction into six coordinated implementation documents. It preserves the useful behavior of the legacy CSense platform while defining the production target for a secure, multi-tenant, cloud-and-edge AI video analytics SaaS platform.

## Documents

| Document | Purpose |
|---|---|
| [01_PRODUCT_REQUIREMENTS_DOCUMENT.md](01_PRODUCT_REQUIREMENTS_DOCUMENT.md) | Product scope, users, functional requirements, non-functional requirements, acceptance criteria, and release boundaries |
| [02_TECHNICAL_REQUIREMENTS_DOCUMENT.md](02_TECHNICAL_REQUIREMENTS_DOCUMENT.md) | Target architecture, service boundaries, security, AI/edge design, APIs, deployment, observability, recovery, and migration |
| [03_APPLICATION_FLOWS.md](03_APPLICATION_FLOWS.md) | End-to-end customer, operator, reseller, camera, incident, model, pipeline, support, and failure flows |
| [04_UI_UX_DESIGN_BRIEF.md](04_UI_UX_DESIGN_BRIEF.md) | Information architecture, screen inventory, interaction patterns, visual direction, privacy, responsive behavior, and accessibility |
| [05_BACKEND_SCHEMA.md](05_BACKEND_SCHEMA.md) | PostgreSQL entities, MongoDB collections, Redis keyspace, object storage, tenancy controls, indexes, retention, and domain events |
| [06_IMPLEMENTATION_PLAN.md](06_IMPLEMENTATION_PLAN.md) | Delivery phases, workstreams, dependencies, quality gates, resourcing, rollout, migration, risks, and Definition of Done |

The following four are operational documents, added once there was a real, running
system to operate — not part of the original six-document spec pack, but published
alongside it for the same reason: real guidance for real operation, not aspirational.

| Document | Purpose |
|---|---|
| [07_OPERATIONS_MANUAL.md](07_OPERATIONS_MANUAL.md) | Admin manual (platform operations: tenants, licenses, support grants, model/pipeline registry) and Operator manual (running the stack: start/stop, logs, migrations, backup/restore, load testing, scaling) |
| [08_API_GUIDE.md](08_API_GUIDE.md) | Authentication, permissions, scoped API keys, error format, pagination, webhooks, and real-time — the concepts that don't fit in the auto-generated OpenAPI documents |
| [09_INCIDENT_RUNBOOKS.md](09_INCIDENT_RUNBOOKS.md) | Ten numbered runbooks (service down, database/Redis outages, license/quota issues, security incidents, failed migrations, storage issues, notification failures, real disaster recovery) — Detect/Diagnose/Mitigate/Resolve/Prevent, each with real commands |
| [10_PRODUCTION_DEPLOYMENT_GUIDE.md](10_PRODUCTION_DEPLOYMENT_GUIDE.md) | What only a human/business can decide (domain, cloud host, secrets, vendor accounts) versus the real, tested mechanism (`docker-compose.prod.yml`, TLS via Traefik/Let's Encrypt) that uses those decisions once made |
| [11_CAMERA_HEALTH_FMEA.md](11_CAMERA_HEALTH_FMEA.md) | Failure Mode and Effects Analysis for the two camera health use cases deliberately not built (`obstruction`, `glare/night-vision`) — failure modes, severity/occurrence/detection scoring, and a validation-data-gated build order, requested as the alternative to guessing detection thresholds |

## Approved Architecture Baseline

The following decisions govern all six documents:

- The target application stack is **FastAPI + Next.js/React**, as confirmed after review of the source documentation.
- The Developer Console uses **Next.js 14** and the Customer CRM uses **React 18 with Vite**. They are separate applications with distinct navigation, permissions, sessions, and operational controls.
- Python remains the primary AI/ML runtime. Rust is optional for future performance-critical edge or media components after profiling; it is not a release-one dependency.
- **Traefik v3** provides ingress, TLS, routing, rate limiting, and security middleware.
- **MediaMTX** manages RTSP, WebRTC, and HLS media transport.
- **PostgreSQL 16** is the system of record for identities, tenants, configuration, licensing, workflow state, and audit indexes.
- **MongoDB 7** stores high-volume detection, telemetry, and incident evidence metadata where flexible event payloads are useful.
- **Redis 7** supports sessions, caching, streams, Pub/Sub, distributed coordination, counters, and configuration invalidation.
- **MinIO-compatible object storage** stores snapshots, clips, model artifacts, exports, and backups.
- Docker Compose supports development, pilot, and smaller deployments. Kubernetes is introduced only when multi-node scale, availability targets, or operational load justify it.
- Existing edge capabilities are retained and modernized: ONVIF discovery, NVR/RTSP connectivity, custom DDNS compatibility, WireGuard, cloud relay, store-and-forward, and controlled temporary support access.

## Requirement Conventions

| Prefix | Area |
|---|---|
| `PRD-FR` | Functional product requirement |
| `PRD-NFR` | Non-functional product requirement |
| `TRD-SEC` | Technical security requirement |
| `TRD-DATA` | Data architecture requirement |
| `TRD-OPS` | Deployment and operations requirement |
| `FLOW` | Application flow |
| `UX` | UI/UX requirement |
| `SCH` | Backend schema rule |
| `IMP` | Implementation milestone or gate |

## Source Reconciliation

The source material contains three architectural generations:

1. The legacy implementation: Nginx, PM2, FastAPI, React SPA, MongoDB/SQLite, custom RTSP proxy, DDNS, and Go/Python edge agent.
2. The containerized modernization baseline: Traefik, separate Admin and Tenant APIs, Next.js console, React CRM, MediaMTX, PostgreSQL, MongoDB, Redis, MinIO, and optional observability overlay.
3. A broad modernization brief that proposes Rust and Angular where appropriate.

The second generation is the implementation target because FastAPI + Next.js/React was subsequently confirmed as final. Legacy edge and camera behavior remains in scope. Rust and Angular are not introduced into the release-one critical path.

## Change Control

Changes affecting tenant isolation, authentication, licensing enforcement, incident integrity, camera credentials, audit logging, or production deployment require architecture and security review. Changes to shared domain contracts must update all affected documents and their referenced requirement IDs.
