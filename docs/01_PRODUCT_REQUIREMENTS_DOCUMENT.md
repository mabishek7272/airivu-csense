# Product Requirements Document

## AIRIVU CSense Enterprise AI Video Analytics Platform

**Version:** 1.0  
**Status:** Implementation baseline  
**Date:** 19 August 2026  
**Owners:** Product, Engineering, Security, AI/ML, Operations  
**Related documents:** [TRD](02_TECHNICAL_REQUIREMENTS_DOCUMENT.md), [Application Flows](03_APPLICATION_FLOWS.md), [UI/UX Brief](04_UI_UX_DESIGN_BRIEF.md), [Backend Schema](05_BACKEND_SCHEMA.md), [Implementation Plan](06_IMPLEMENTATION_PLAN.md)

---

## 1. Executive Summary

AIRIVU CSense is an enterprise, multi-tenant AI video analytics platform that converts existing CCTV, IP camera, and NVR infrastructure into real-time safety, security, operational, and compliance intelligence.

The modernization will retain working CSense capabilities such as RTSP processing, ONVIF discovery, DDNS, edge agents, AI detection, snapshots, WebSocket alerts, and customer camera management. It will replace tightly coupled deployment and data patterns with separate customer and operator applications, explicit service boundaries, strict tenant isolation, versioned AI pipelines, auditable privileged access, resilient edge coordination, centralized notifications, licensing, APIs, and operational observability.

The product has two deliberately separate experiences:

- **Customer CRM:** tenant-scoped management of sites, cameras, edge devices, incidents, notifications, users, reports, and integrations.
- **Developer/Platform Console:** controlled platform operations across tenants, including organization and license administration, model and pipeline lifecycle, system health, API operations, security, deployment, testing, and support access.

## 2. Product Vision

Enable organizations to deploy trustworthy AI video intelligence on existing camera infrastructure without surrendering privacy, tenant isolation, operational control, or deployment flexibility.

## 3. Product Outcomes

The first production release must achieve these outcomes:

1. An organization can be licensed, onboarded, and isolated without manual database intervention.
2. A tenant administrator can connect a site, edge device, NVR, or camera and assign a validated AI pipeline.
3. The platform can detect an event, create an incident with evidence, notify authorized recipients, and track acknowledgement through resolution.
4. Platform operators can deploy and roll back models and pipelines with full version history and audit evidence.
5. The system can continue edge processing during intermittent cloud connectivity and synchronize safely when connectivity returns.
6. Every privileged action is attributable, reviewable, and exportable.
7. Resource use is constrained by the active tenant or reseller license at the backend.

## 4. Problem Statement

The legacy platform demonstrates viable AI video analytics but is difficult to scale and operate as an enterprise SaaS product because:

- Several responsibilities are concentrated in a small number of backend processes.
- SQLite, MongoDB, local files, and runtime state have overlapping ownership.
- Customer and internal operator capabilities are insufficiently separated.
- Tenant isolation and license enforcement require stronger data-layer controls.
- Model and pipeline changes need versioning, promotion gates, rollback, and provenance.
- Edge, DDNS, NVR, relay, and direct-connect workflows need a single managed trust model.
- Local snapshots and custom streaming processes limit horizontal scale.
- Audit, observability, recovery, and automated deployment need enterprise controls.

## 5. Goals

| ID | Goal | Success indicator |
|---|---|---|
| G-01 | Secure multi-tenancy | Automated isolation tests show zero cross-tenant access |
| G-02 | Reliable incident detection and delivery | 95% of eligible high-severity events reach the tenant UI within 5 seconds under supported load |
| G-03 | Edge-first processing | Raw video remains on site by default; metadata/evidence sync follows policy |
| G-04 | Controlled AI lifecycle | Every active pipeline maps to immutable model and configuration versions |
| G-05 | Operational accountability | 100% of privileged mutations create an audit record |
| G-06 | API-first extensibility | All supported UI operations use documented versioned APIs |
| G-07 | Commercial control | License entitlements and quotas are enforced server-side |
| G-08 | Deployment flexibility | Same product supports pilot Compose, enterprise Kubernetes, edge, and on-prem profiles |

## 6. Non-Goals for Release One

- Replacing customer CCTV/NVR systems with a proprietary recorder.
- Continuous central cloud recording of all raw camera streams.
- Fully autonomous emergency dispatch without customer-defined approval and escalation policies.
- Facial recognition by default. Identity-sensitive models require a separate legal, privacy, and product approval path.
- Blockchain as an operational database.
- A public AI model marketplace.
- Full billing ledger, taxation, or payment collection; release one exposes subscription and usage data to an external billing system.
- Native mobile applications; responsive web and API readiness are required.

## 7. Users and Roles

| Persona | Scope | Primary needs |
|---|---|---|
| Principal Administrator | Whole platform | Organizations, developers, licenses, security, models, platform analytics, audit |
| Platform Developer/Operator | Permission-controlled cross-tenant access | Troubleshooting, deployment, model/pipeline operations, API and system health |
| Reseller Super Admin | Reseller organization and child tenants | Create/manage customers within license limits, view aggregate usage and support status |
| Tenant Super Admin | Single tenant | Full tenant configuration, roles, sites, cameras, policies, integrations |
| Tenant Admin | Assigned tenant/site scope | Daily operations, cameras, incidents, users, reports |
| Operator/User | Assigned sites/cameras | Live view, incident review, acknowledgement, comments, permitted reports |
| Auditor/Read-only | Defined tenant or platform scope | Search and export evidence, configuration history, and audit trails |
| Edge Device | Machine identity | Secure enrollment, configuration, heartbeat, AI execution, event synchronization |
| API Client | Service identity | Scoped API and webhook access under tenant entitlements |

## 8. Product Scope

### 8.1 Customer CRM modules

- Authentication, MFA, recovery, active sessions
- Dashboard and site health
- Sites, zones, cameras, NVRs, and edge devices
- Live and multi-camera view
- AI pipeline assignment and tenant-level tuning within allowed ranges
- Incidents, evidence, acknowledgement, escalation, comments, assignment, and closure
- Notifications, recipients, schedules, and channel preferences
- Users, roles, site scope, and service accounts
- Reports, exports, analytics, and retention visibility
- API keys, webhooks, and integration status
- License, quota, usage, and feature visibility
- Tenant settings, privacy, and data retention policies

### 8.2 Developer/Platform Console modules

- Global platform health and operational overview
- Organizations, reseller hierarchy, tenant state, suspension, and usage
- Developer accounts, roles, approvals, and just-in-time support access
- License plans, license instances, entitlements, quotas, and enforcement status
- Model registry, artifacts, versions, validation, promotion, deployment, and rollback
- Pipeline builder, versioning, test runs, assignments, deployment, and rollback
- Camera/edge fleet diagnostics and controlled support connectivity
- Notification providers, templates, delivery logs, retry queues, and failures
- API catalog, keys, rate policies, webhooks, traffic, logs, and documentation
- Audit search, security events, exports, and privileged-action review
- Test status, release evidence, deployment status, and rollback controls
- Observability dashboards and service-level objective status

## 9. Functional Requirements

### 9.1 Identity, authentication, and authorization

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| PRD-FR-001 | The platform shall support separate authentication contexts for Customer CRM and Developer Console. | Must | A customer token cannot call operator APIs; an operator token cannot silently inherit tenant access. |
| PRD-FR-002 | MFA shall be mandatory for principal administrators and developers, and configurable/forceable for tenant users. | Must | Protected roles cannot complete login without enrolled MFA or approved recovery. |
| PRD-FR-003 | Roles shall be backed by granular permissions and resource scopes. | Must | API authorization validates permission, tenant, site, and object scope. |
| PRD-FR-004 | Users shall view and revoke active sessions; administrators shall revoke sessions within their authority. | Must | Revoked refresh tokens cannot be replayed. |
| PRD-FR-005 | Service accounts and API clients shall use scoped, rotatable credentials that are distinct from human accounts. | Must | Secret is shown once, stored hashed, expires/rotates, and is auditable. |

### 9.2 Organization, tenant, reseller, and licensing

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| PRD-FR-010 | Principal administrators shall create direct tenants and reseller organizations. | Must | Creation requires license, owner, region, retention, and security settings. |
| PRD-FR-011 | Resellers shall create child tenants only within active license entitlements. | Must | Over-limit requests fail atomically with an actionable quota response. |
| PRD-FR-012 | Licenses shall support quarterly, half-yearly, and yearly terms. | Must | Effective dates, expiry, grace state, suspension state, and renewal history are retained. |
| PRD-FR-013 | Limits shall cover cameras, users, storage, edge devices, pipelines, child tenants, API usage, and features. | Must | Enforcement is server-side and race-safe. |
| PRD-FR-014 | Administrators shall see current use, allocated limits, forecast exhaustion, and blocked actions. | Must | Dashboard and API expose consistent quota values. |
| PRD-FR-015 | Suspension shall prevent new operations while preserving evidence and an auditable recovery path. | Must | Suspension behavior is policy-driven and reversible by authorized roles. |

### 9.3 Sites, edge devices, NVRs, and cameras

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| PRD-FR-020 | Tenant admins shall create sites, zones, and camera groups. | Must | Every camera belongs to a tenant and site; zone membership is optional and scoped. |
| PRD-FR-021 | The platform shall support ONVIF discovery, manual RTSP, NVR channels, and approved DDNS endpoints. | Must | Discovered credentials are not exposed; duplicate endpoints are detected. |
| PRD-FR-022 | Edge devices shall enroll using short-lived bootstrap credentials and receive a unique machine identity. | Must | Enrollment rotates the bootstrap secret and establishes mutual authentication. |
| PRD-FR-023 | Camera credentials shall be encrypted and delivered only to authorized edge/media components. | Must | UI and normal APIs never return the stored secret. |
| PRD-FR-024 | Camera health shall include reachable, stream status, FPS, latency, obstruction/tamper indicators, and last event. | Must | Health transitions create telemetry and optional notification events. |
| PRD-FR-025 | Controlled temporary direct access shall require explicit reason, duration, authorization, and audit. | Should | Access expires automatically and displays a visible tenant notification when policy requires. |

### 9.4 Live video and privacy

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| PRD-FR-030 | Authorized users shall view live video using WebRTC with HLS fallback. | Must | Stream access is short-lived, scoped, revocable, and never exposes source credentials. |
| PRD-FR-031 | Multi-camera layouts shall support saved views subject to browser/device capacity. | Should | Layouts retain camera references but not credentials or unrestricted media URLs. |
| PRD-FR-032 | Privacy modes shall include face/person masking and privacy-preserving silhouettes where configured. | Should | Original imagery is restricted by policy; masked live/evidence view is the default in protected zones. |
| PRD-FR-033 | Snapshot and clip access shall use expiring authorized URLs and be logged for sensitive evidence. | Must | Direct bucket access is unavailable. |

### 9.5 AI models and pipelines

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| PRD-FR-040 | Developers shall register model metadata and immutable artifact versions. | Must | Artifact digest, framework, task, labels, owner, provenance, and validation state are recorded. |
| PRD-FR-041 | Model versions shall pass compatibility, security, functional, accuracy, and performance gates before production promotion. | Must | Failed versions cannot be deployed to production. |
| PRD-FR-042 | Pipelines shall be composed from versioned stages: ingestion, preprocessing, inference, filtering, ROI, tracking, rules, incident, evidence, and notification. | Must | Published versions are immutable and reproducible. |
| PRD-FR-043 | Pipeline updates shall support canary rollout, health comparison, pause, rollback, and hot configuration reload. | Must | Rollback restores the prior version without data loss. |
| PRD-FR-044 | Camera-to-pipeline assignments shall specify effective time, runtime target, and configuration version. | Must | A camera resolves to one deterministic effective assignment per use case. |
| PRD-FR-045 | Tenant admins may tune only parameters exposed by the pipeline policy and within approved ranges. | Should | Invalid or unauthorized overrides are rejected and audited. |

### 9.6 Detection, incident, and evidence lifecycle

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| PRD-FR-050 | Eligible AI detections shall be normalized into domain events with tenant, site, camera, model, pipeline, timestamps, confidence, and trace ID. | Must | Required fields are validated before incident processing. |
| PRD-FR-051 | Rules shall correlate detections, suppress duplicates, apply schedules/ROIs, and create or update incidents. | Must | Suppression and correlation decisions remain explainable. |
| PRD-FR-052 | Incidents shall support status, severity, assignee, acknowledgement, escalation, comments, evidence, resolution, and immutable history. | Must | Every state change has actor, timestamp, reason, and prior/new state. |
| PRD-FR-053 | Evidence shall retain a verifiable link to source event, camera, model, pipeline, and storage object. | Must | Evidence deletion follows policy and produces an audit record. |
| PRD-FR-054 | Operators shall filter incidents by time, site, camera, use case, severity, status, and assignee. | Must | Search results respect tenant and site scope. |
| PRD-FR-055 | False-positive and outcome labels shall feed an approved model improvement workflow without directly retraining production. | Should | Labels are separated from production model deployment. |

### 9.7 Notification center

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| PRD-FR-060 | The platform shall support in-app, web push, email, SMS, and webhook channels through a common notification model. | Must | Channel providers are replaceable and failure-isolated. |
| PRD-FR-061 | Notification policies shall support severity, schedule, recipient group, escalation delay, acknowledgement, retry, and fallback. | Must | Policy simulation shows recipients and channels before activation. |
| PRD-FR-062 | Delivery attempts shall record accepted, sent, delivered, failed, retried, suppressed, and acknowledged states where providers support them. | Must | Technical logs do not expose message secrets. |
| PRD-FR-063 | Tenants shall control operational recipients; platform operators shall manage provider health and templates within their authority. | Must | Provider credentials remain platform-side and encrypted. |

### 9.8 APIs, webhooks, and integrations

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| PRD-FR-070 | Supported capabilities shall be available through versioned REST APIs with OpenAPI specifications. | Must | UI uses the same supported APIs; undocumented privileged endpoints are prohibited. |
| PRD-FR-071 | Real-time incident and health updates shall be available through authenticated WebSockets. | Must | Connections are tenant-scoped and revocable. |
| PRD-FR-072 | Tenants shall configure signed webhooks with event filters, retries, delivery history, and secret rotation. | Must | Receiver can verify timestamp and signature; replay window is enforced. |
| PRD-FR-073 | APIs shall return correlation IDs, stable error codes, validation details, and rate-limit headers. | Must | Support can trace a request without exposing sensitive payloads. |
| PRD-FR-074 | API usage shall count against the correct license and rate policy. | Must | Distributed counters are consistent within defined tolerance. |

### 9.9 Audit, security, and support access

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| PRD-FR-080 | All privileged and security-sensitive actions shall emit centralized audit events. | Must | Required audit coverage is verified in automated tests. |
| PRD-FR-081 | Audit records shall capture actor, scope, action, target, outcome, time, session/device context, correlation ID, and redacted before/after values. | Must | Secrets and raw tokens are never stored in audit payloads. |
| PRD-FR-082 | Developer access to tenant data shall require purpose, scope, duration, approval policy, and visible session status. | Must | Access expires automatically and all reads/mutations are attributable. |
| PRD-FR-083 | Impersonation, if enabled, shall use a distinct support session banner and shall never reveal credentials or bypass authorization. | Should | Impersonated actions show both developer and represented user context. |
| PRD-FR-084 | High-value audit records shall support hash chaining or periodic tamper-evident anchoring. | Should | Integrity verification detects altered or missing chain elements. |

### 9.10 Reporting and analytics

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| PRD-FR-090 | Tenants shall access incident, camera health, use-case, response-time, and notification reports. | Must | Report data matches scoped operational records. |
| PRD-FR-091 | Exports shall be asynchronous, access-controlled, time-limited, and auditable. | Must | Large exports do not block interactive APIs. |
| PRD-FR-092 | Platform analytics shall show tenant/resource growth, fleet health, AI throughput, API use, incidents, and license utilization. | Must | Platform views aggregate without leaking tenant evidence. |

## 10. Non-Functional Requirements

| ID | Category | Requirement / target |
|---|---|---|
| PRD-NFR-001 | Availability | Customer control plane target: 99.9% monthly after production stabilization; pilot target: 99.5%. |
| PRD-NFR-002 | Alert latency | p95 from accepted edge/detection event to in-app publish under 5 seconds, excluding customer/provider network delays. |
| PRD-NFR-003 | API performance | p95 read API under 500 ms and mutation API under 1 second for normal payloads at supported load. |
| PRD-NFR-004 | Scale | Initial production design: 100 tenants, 5,000 cameras, 500 concurrent users, and 1,000 detection events/second burst; components must scale horizontally. |
| PRD-NFR-005 | Isolation | Every tenant-owned record and event carries tenant identity; automated negative tests cover IDOR and cross-tenant joins. |
| PRD-NFR-006 | Recovery | Production target RPO ≤15 minutes for control data and RTO ≤4 hours; edge store-and-forward protects unsynchronized events. |
| PRD-NFR-007 | Security | TLS 1.2+, encrypted secrets/data at rest, MFA for privileged roles, OWASP-aligned controls, dependency/image scanning, and annual penetration testing. |
| PRD-NFR-008 | Observability | Metrics, structured logs, traces, health checks, dashboards, and alerting for every production service. |
| PRD-NFR-009 | Accessibility | Customer and operator web applications meet WCAG 2.2 AA for supported workflows. |
| PRD-NFR-010 | Browser support | Current and previous major versions of Chrome, Edge, Firefox, and Safari; degraded behavior is documented. |
| PRD-NFR-011 | Data residency | Tenant deployment region and evidence retention are configurable within available infrastructure regions. |
| PRD-NFR-012 | Maintainability | Versioned contracts, automated migrations, ≥80% unit coverage on critical domain modules, and no production change without rollback. |
| PRD-NFR-013 | Privacy | Data minimization, configurable masking, retention, access logging, and purpose-specific use-case activation. |
| PRD-NFR-014 | Time | Persist UTC; display user/tenant timezone. Edge events include capture time, receive time, and clock-offset health. |

## 11. Primary Use Cases

Release one must support configurable pipelines for the existing use-case families:

- Intrusion, perimeter crossing, loitering, restricted-zone entry, and tailgating
- Aggression, fall detection, crowding, queue monitoring, and abandoned objects
- Fire and smoke
- PPE, housekeeping, kitchen safety, and operational compliance
- ANPR/LPR, parking, vehicle deviation, and asset tampering
- Camera health: offline, obstruction, glare, night-vision failure, low FPS, storage/network failure

Each use case is activated only after model validation, customer configuration, legal/privacy review where necessary, and explicit camera/pipeline assignment.

## 12. Key Product Rules

1. The backend derives tenant scope from the authenticated identity; it does not trust tenant IDs supplied by a client.
2. A published pipeline or model artifact is immutable. Changes create a new version.
3. A license denial is atomic and cannot be overridden by frontend behavior.
4. Raw camera credentials and long-lived media URLs never appear in client responses.
5. Platform support access is temporary, attributable, and visible according to policy.
6. Edge processing is preferred. Cloud processing is selected by capability, customer policy, and capacity.
7. Incidents are business records; detections are technical observations. One incident may correlate many detections.
8. Audit logging is part of the transaction or reliable outbox flow, not a best-effort UI activity.

## 13. Customer Journey

1. Commercial agreement and license are created.
2. Organization owner receives a time-limited invitation.
3. Owner completes MFA and tenant security setup.
4. Tenant creates sites, zones, users, and notification contacts.
5. Edge gateway is enrolled or a supported direct/NVR connector is configured.
6. Cameras are discovered, verified, named, grouped, and assigned privacy policies.
7. Validated AI pipelines are assigned and tested in observe-only mode.
8. Alerts are activated with recipient and escalation rules.
9. Tenant reviews incidents, reports, quota, and camera health.
10. Customer success reviews tuning and production readiness.

## 14. Success Metrics

| Area | Metric |
|---|---|
| Onboarding | Median time from edge power-on to first verified camera under 30 minutes for supported networks |
| Reliability | ≥99% successful edge configuration synchronization excluding offline devices |
| Detection operations | p95 event-to-UI latency under 5 seconds at supported load |
| Incident response | Acknowledgement and resolution time measurable for ≥99% of created incidents |
| Notification | ≥98% platform-to-provider acceptance for valid messages; provider delivery tracked separately |
| Security | Zero unresolved critical vulnerabilities at production release |
| Audit | 100% coverage for the privileged-action catalog |
| Deployment | Production rollback or forward-fix decision possible within 30 minutes of failed release gate |
| Customer experience | ≥90% successful completion in onboarding and incident usability tests |

## 15. Release Acceptance Criteria

Release one is ready for controlled production when:

- Two independent tenants complete onboarding and cannot access one another's data under automated and manual tests.
- At least one edge deployment processes a supported AI use case during cloud disconnection and synchronizes events after recovery.
- Camera onboarding works through ONVIF and manual RTSP/NVR channel paths.
- A model and pipeline move through draft, validation, staging, canary, production, and rollback.
- A detection creates a correlated incident, evidence object, notification, acknowledgement, and final resolution with traceable history.
- License limits remain correct under concurrent create requests.
- Privileged support access requires approval/purpose and produces complete audit history.
- Backup restore is demonstrated in a clean environment within the target RTO.
- Security, tenant isolation, performance, failure recovery, and migration test gates pass.
- Operational runbooks, ownership, alerts, dashboards, and support escalation are active.

## 16. Dependencies and Assumptions

- Customer networks permit an approved outbound secure connection from the edge gateway.
- Camera/NVR vendors provide valid RTSP/ONVIF behavior or documented proprietary adapters.
- Model licenses permit intended commercial deployment and redistribution method.
- SMS, email, and push delivery depend on contracted external providers.
- Production sizing will be recalibrated using pilot telemetry before enterprise rollout.
- Applicable privacy and CCTV laws vary by deployment country and customer sector; tenant configuration does not replace legal review.

## 17. Product Risks

| Risk | Product response |
|---|---|
| Camera and NVR fragmentation | Certification matrix, adapter interface, lab devices, and explicit unsupported states |
| False positives create alert fatigue | Observe-only tuning, deduplication, schedules, feedback, and escalation thresholds |
| Central streaming creates cost/privacy pressure | Edge-first inference, policy-controlled evidence, and temporary live relay |
| Excessive operator privilege | Separate console, least privilege, MFA, approval, JIT access, and audit |
| Reseller hierarchy complicates isolation | One tenant boundary per customer; reseller aggregation through explicit relationships, not shared records |
| Premature Kubernetes complexity | Compose first; introduce Kubernetes only at documented scale/availability trigger |
| Technology divergence | FastAPI + Next.js/React remains baseline; new languages require an architecture decision record and measured need |

## 18. Traceability Summary

| Product area | PRD requirements | Detailed design |
|---|---|---|
| Identity and tenancy | PRD-FR-001–005, PRD-NFR-005 | TRD sections 6–8; Schema sections 3–5 |
| Licensing and reseller | PRD-FR-010–015 | TRD section 9; Schema section 6 |
| Edge/camera/media | PRD-FR-020–033 | TRD sections 12–14; Flows 4–6 |
| Models and pipelines | PRD-FR-040–045 | TRD sections 15–16; Flows 9–10 |
| Incidents/evidence | PRD-FR-050–055 | TRD section 17; Schema sections 7–8 |
| Notifications | PRD-FR-060–063 | TRD section 18; Flow 8 |
| APIs and integrations | PRD-FR-070–074 | TRD sections 10–11; Schema section 12 |
| Audit and support | PRD-FR-080–084 | TRD sections 7 and 19; Flow 12 |
| Reporting | PRD-FR-090–092 | TRD section 20; UX screen inventory |

