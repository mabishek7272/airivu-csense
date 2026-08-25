# UI/UX Design Brief

## AIRIVU CSense Customer CRM and Developer Console

**Version:** 1.0  
**Date:** 19 August 2026  
**Audience:** Product design, UX research, frontend engineering, product management, security, QA

---

## 1. Design Objective

Design two related but operationally distinct enterprise applications for AI video analytics:

- **Customer CRM:** calm, fast, privacy-aware situational control for tenant users managing sites, cameras, alerts, evidence, and people.
- **Developer Console:** dense but disciplined platform control for trusted operators managing tenants, licenses, AI assets, deployments, health, APIs, and security.

Both applications should feel like one AIRIVU product family, but they must never look like the same dashboard with hidden menu items. Different navigation, terminology, access context, risk cues, and session controls should make the current mode unmistakable.

## 2. Experience Principles

1. **Incidents before metrics:** urgent actionable events take visual priority over decorative analytics.
2. **State must be explicit:** online, stale, offline, degraded, masked, recording, processing, acknowledged, and failed are never communicated by color alone.
3. **Progressive disclosure:** show the operational decision first; technical diagnostics remain one level deeper.
4. **Privacy is visible:** masking, evidence access, support sessions, retention, and data export states are clearly labeled.
5. **Risk-aware actions:** ordinary work is efficient; sensitive actions require reason, preview, confirmation, or step-up authentication.
6. **No mystery automation:** AI confidence, rule, pipeline version, and why an incident was created are inspectable.
7. **Recoverable workflows:** long operations are resumable, failures are actionable, and drafts are preserved.
8. **High-density without clutter:** enterprise tables may be information-rich but use grouping, hierarchy, filters, and saved views.
9. **Accessible operations:** keyboard, screen reader, contrast, motion, focus, and zoom behavior meet WCAG 2.2 AA.

## 3. Product Identity and Visual Direction

### 3.1 Character

The visual language should feel precise, vigilant, and trustworthy. Avoid science-fiction decoration, neon overload, fake glass interfaces, excessive gradients, and animated dashboards that compete with live operational content.

### 3.2 Color roles

Final brand tokens should be drawn from the approved AIRIVU/3RDi identity. Functional roles must remain stable:

| Role | Usage |
|---|---|
| Primary | Navigation selection, primary actions, active controls |
| Neutral | Application canvas, cards, borders, disabled states |
| Informational blue | Processing, connected, assigned, informational notices |
| Success green | Healthy, delivered, resolved, validated |
| Warning amber | Degraded, nearing quota, needs attention, canary |
| Critical red | Active critical incident, failed security/deployment, revoked |
| Privacy violet | Masked/silhouette, restricted evidence, privacy policy |

Severity color must always be paired with icon and text. Do not use red for ordinary destructive-looking brand decoration.

### 3.3 Typography and density

- Use a highly legible sans-serif family with tabular numbers.
- Base text 14–16 px depending on density mode; never below 12 px for operational labels.
- Provide comfortable and compact density for tables; customer default is comfortable, console default may be compact.
- Use monospace selectively for IDs, versions, hashes, endpoints, logs, and correlation IDs.

### 3.4 Icons

Use one consistent outlined icon family. Icons supplement text and never carry the only meaning. Camera, site, edge, model, pipeline, incident, privacy, evidence, webhook, deployment, and audit should have distinct shapes.

## 4. Application Shells

### 4.1 Customer CRM shell

**Desktop layout:**

- Left navigation: Dashboard, Live, Incidents, Sites & Cameras, Reports, Administration.
- Top bar: organization/site context, global search, connection status, notifications, help, user menu.
- Content header: title, scope, freshness timestamp, primary action.
- Optional right inspector for incident/camera details without losing list context.

**Persistent cues:** active tenant, site scope, live/offline status, open critical incident count, and privacy state where relevant.

### 4.2 Developer Console shell

- Left navigation grouped by Platform, Organizations, AI Control Plane, Fleet, Integrations, Operations, Security, Releases.
- Top bar: environment selector, global command/search, incident status, support-grant indicator, notifications, privileged identity.
- Environment and tenant support context use prominent banners and cannot be hidden while active.
- Production mutations carry a clear `Production` label and may require step-up.

### 4.3 Mobile/responsive behavior

Customer CRM supports responsive operational use:

- Bottom or compact navigation for Dashboard, Incidents, Live, More.
- Incident cards replace wide tables.
- One live stream at a time by default.
- Acknowledge, call/escalate, comment, and resolve remain thumb-accessible.
- Complex camera setup and policy builders may recommend desktop but remain viewable.

Developer Console is desktop-first. On small screens, allow read-only health, audit review, and incident acknowledgement; block unsafe configuration editing when layout cannot present required context.

## 5. Information Architecture

### 5.1 Customer CRM

| Navigation | Screens |
|---|---|
| Dashboard | Tenant overview, site health, active incidents, camera health, response metrics, quota summary |
| Live | Camera grid, saved views, map/floor plan, single camera inspector |
| Incidents | Inbox, detail, timeline, evidence, assignments, escalation, closed incidents |
| Sites & Cameras | Sites, zones, cameras, NVRs, edge devices, discovery, health, pipelines |
| Reports | Incident analytics, response time, use case, camera uptime, scheduled reports, exports |
| Administration | Users/roles, notification policies, integrations, license/usage, privacy/retention, audit |

### 5.2 Developer Console

| Navigation | Screens |
|---|---|
| Platform | Overview, service health, global metrics, active operational incidents |
| Organizations | Tenants, resellers, child allocations, licenses, usage, suspension |
| AI Control Plane | Model registry, validation runs, pipeline builder, versions, assignments, deployments |
| Fleet | Edge devices, cameras, software/model versions, diagnostics, support connectivity |
| Integrations | API catalog, keys, traffic, webhooks, providers, templates, delivery logs |
| Operations | Queues, jobs, databases, object storage, traces, logs, backups, restore status |
| Security | Developers, permissions, support grants, security events, audit, policy |
| Releases | Builds, test evidence, environments, deployment, canary, rollback |

## 6. Customer Screen Specifications

### UX-CRM-01: Tenant Dashboard

**Purpose:** Answer: What needs attention now, what is unhealthy, and are we operating within limits?

Layout:

1. Critical/major incident strip with acknowledge CTA.
2. Site health summary: healthy, degraded, offline.
3. Camera health: online/offline/blocked/low FPS/stream error.
4. Response metrics: open, unacknowledged, median acknowledgement, SLA risk.
5. Recent incident feed with masked thumbnails.
6. License usage summary with forecast warning.
7. System freshness and last successful sync.

Avoid vanity metrics such as total detections without context. Each card must lead to a filtered operational view.

### UX-CRM-02: Live View

Features:

- 1, 2, 4, 9, and responsive grid layouts.
- Saved layouts by user or team.
- Site/group filter and camera search.
- Stream tile: name, site, health, latency, active use cases, privacy badge, full-screen, snapshot if permitted.
- Single camera inspector: stream profiles, pipeline, recent incidents, health timeline.
- Reconnect and fallback states with actionable diagnostics.

Privacy:

- Masked/silhouette mode displays a persistent badge.
- Users without unmask permission do not see a reveal control.
- Authorized reveal, if allowed, requires reason/step-up and is time-limited and audited.
- Protected rooms use privacy-preserving silhouettes or skeletal/identifier overlays according to policy; no facial detail should appear.

### UX-CRM-03: Incident Inbox

Default columns/cards:

- Severity
- Status
- Type/use case
- Site/camera
- Detected time and age
- Masked preview
- Assignee
- Acknowledgement state
- Notification/escalation state

Interaction:

- Saved filters and views
- Multi-select assignment/acknowledgement only when policy permits
- Real-time insertion without moving the user's active keyboard focus
- New-items banner if automatic insertion would disrupt review
- Stale-data and reconnect indicator

### UX-CRM-04: Incident Detail

Use a three-region layout:

- **Primary evidence:** image/clip with privacy state, zoom, timestamp, camera, digest/status.
- **Decision panel:** severity, state, acknowledge, assign, escalate, resolve, false-positive label.
- **Timeline:** detections, rule explanation, notifications, user actions, comments, evidence, state history.

Include **Why this incident exists**:

- Use case and rule
- Model/pipeline version
- Confidence and threshold
- ROI/schedule
- Correlation/suppression summary

Raw model diagnostics are collapsed for ordinary users.

### UX-CRM-05: Sites and Camera Inventory

Provide table and site-card views. Required fields: camera name, site/zone, edge/NVR, connection state, last frame, FPS, pipeline, privacy, software/model status, last incident, and actions.

Bulk actions require a preview of affected cameras and version conflict handling.

### UX-CRM-06: Camera Onboarding Wizard

Steps:

1. Choose site and connection method.
2. Select edge/NVR or manual endpoint.
3. Discover and select devices/channels.
4. Enter credentials securely and test.
5. Select stream profiles and time settings.
6. Name/group/zone cameras.
7. Set privacy and retention.
8. Assign pipeline in observe-only or active mode.
9. Review quota impact and confirm.
10. Verify health and first test event.

The wizard saves resumable progress. Password fields never repopulate from stored credentials.

### UX-CRM-07: Edge Device Detail

Show identity, site, online state, last heartbeat, desired/observed config, agent version, certificate expiry, CPU/GPU/RAM/storage/temperature, spool use, cameras, active pipelines, recent commands, and health timeline.

Remote support actions are absent unless the user has permission; they explain scope, duration, tenant visibility, and audit.

### UX-CRM-08: Notification Policy Builder

Use a step-based builder:

- Trigger and severity
- Schedule/quiet hours
- Recipient groups
- Initial channels
- Escalation steps
- Acknowledgement behavior
- Failure fallback
- Simulation and publish

Represent escalation as a vertical timeline, not a free-form canvas. Detect missing contacts and contradictory rules before publish.

### UX-CRM-09: Users and Roles

- User list with status, MFA, memberships, site scopes, last activity, invitation state.
- Role matrix shows permissions by resource/action.
- High-risk permissions are grouped and explained.
- Removing the final Super Admin requires ownership transfer.
- Session revocation and access changes show impact before confirmation.

### UX-CRM-10: License and Usage

Show effective plan, dates, grace/suspension policy, entitlements, current use, reserved use, remaining capacity, forecast, and recent denials. Reseller view adds child allocations and unallocated capacity.

## 7. Developer Console Screen Specifications

### UX-CON-01: Platform Overview

Prioritize:

- Active platform incidents and SLO/error-budget status
- API/error/latency summaries
- Event and notification queue age
- Online/degraded/offline edge fleet
- Camera connectivity and AI throughput
- Deployment/canary status
- Database/object storage/backup health
- License or tenant anomalies

Every metric links to a scoped diagnostic screen. Avoid auto-refresh that resets filters or selection.

### UX-CON-02: Organization Detail

Tabs:

- Summary
- License and quota
- Sites/fleet summary
- Usage
- Security policy
- Integrations
- Audit
- Support access

Evidence and personal data are not shown by default. A support-grant flow is required to enter tenant-sensitive views.

### UX-CON-03: License Editor

Display plan defaults and overrides distinctly. Preview effective entitlements, reseller allocation impact, expiry behavior, and affected resources before publish. Concurrent changes use version comparison.

### UX-CON-04: Model Registry

Registry table: family, task, latest version, stage, framework, hardware, validation, deployment count, owner, updated time.

Model version page:

- Model card and provenance
- Artifact digest and compatibility
- Validation gates and metrics
- Dataset references
- Resource benchmark
- Deployments and pipeline dependencies
- Promotion/deprecation actions

Production promotion requires a review panel and cannot be a one-click table action.

### UX-CON-05: Pipeline Builder

Use a left stage library, central ordered pipeline, and right configuration inspector. The builder is not a decorative node graph; stage order and data contract must be unambiguous.

Capabilities:

- Add/reorder supported stages
- Pin exact model version
- Configure threshold, ROI behavior, FPS, batching, tracking, rules, evidence, incident, notification
- Define allowed tenant overrides/ranges
- View schema errors and hardware estimate
- Test against recorded samples
- Compare versions
- Submit for approval

### UX-CON-06: Deployment and Canary

Show target, version, cohort, progress, observed/desired count, health metrics, guardrails, events, and prior version. Primary actions: pause, expand, roll back. Require reason for production action and show impact estimate.

### UX-CON-07: Fleet Operations

Table supports filters for tenant, site, device/camera, status, version, certificate, hardware, region, spool, last heartbeat, and active deployment. Bulk commands are limited to safe cohorts and show expiry plus rollback.

### UX-CON-08: API Operations

Screens:

- API catalog and OpenAPI versions
- Traffic and latency by client/tenant/endpoint
- Rate-limit policy
- Platform service clients
- Webhook deliveries
- Request lookup by correlation ID

Payload inspection must be permission-controlled and redacted by default.

### UX-CON-09: Audit Explorer

Filters: time, actor, actor type, tenant, support grant, action, resource, outcome, IP/device, correlation ID, risk level.

Detail drawer shows immutable event metadata, redacted before/after diff, related events, integrity status, and export action. Audit records cannot be edited from the UI.

### UX-CON-10: Support Grant

Form requires tenant, ticket/reference, purpose, requested scopes, sites/resources, start, duration, and optional approver. The active grant creates a persistent banner:

`Support access: Tenant Name · evidence.read · expires in 24 min · End session`

No tenant-sensitive content can appear in browser history labels, notification previews, or unrelated search results after the grant ends.

## 8. Shared Component Inventory

| Component | Requirements |
|---|---|
| App shell | Skip link, keyboard navigation, stable focus, responsive, mode/environment banner |
| Data table | Column control, sorting, server filters, saved views, cursor pagination, bulk selection, accessible headers |
| Status badge | Icon + text + semantic color; tooltip provides definition and timestamp |
| Health summary | Aggregate plus explicit unknown/stale category |
| Evidence viewer | Masking, access state, timestamp, digest/status, keyboard controls, no uncontrolled download |
| Timeline | Actor, event, time, context, diff, expandable technical details |
| Command palette | Navigation and read actions; risky mutations are not executed silently |
| Confirmation dialog | Action, scope, impact, reversibility, reason, step-up state |
| Async job panel | Queued/running/progress/succeeded/failed/cancelled/expired and correlation ID |
| Filter bar | Search, structured filters, active chips, clear all, share/save view |
| Empty state | Explain why empty and the authorized next action |
| Error state | Safe message, stable code, correlation ID, retry eligibility, support path |
| Diff viewer | Before/after or version comparison with secret redaction |
| Quota meter | Used/reserved/limit/forecast with exact numbers and accessible text |
| JSON/log viewer | Search, wrap, copy permitted fields, redaction, virtual scrolling |

## 9. State and Feedback Standards

### 9.1 Loading

- Use skeletons only where structure is known.
- Preserve previous safe data during background refresh and label it `Updating`.
- Operations longer than 2 seconds show progress or durable queued state.

### 9.2 Empty

Differentiate:

- No data created yet
- No results for current filter
- Data outside selected time range
- No permission
- Feature unavailable under license
- Edge/device offline

### 9.3 Errors

Messages explain what failed, what remained unchanged, whether retry is safe, and correlation ID. Never show raw stack traces, secrets, camera URLs, database names, or provider credentials.

### 9.4 Optimistic updates

Use only for low-risk reversible actions. Incident acknowledgement, license allocation, pipeline deployment, access changes, and evidence actions require confirmed server response.

### 9.5 Real-time updates

- Announce critical updates accessibly without stealing focus.
- If list order would change while a user is reading, show a `New updates` control.
- Show last event time and connection/reconnect state.
- On reconnect, fetch authoritative state before applying buffered events.

## 10. Privacy and Evidence Design

1. Use masked thumbnails by default in incident lists and notifications.
2. Protected-zone mode supports full blur, face/person masking, silhouette, skeletal pose, or metadata-only views.
3. Privacy labels identify `Masked by policy`, `Original restricted`, `Temporary reveal`, and `No image retained`.
4. Original evidence access can require a reason, recent MFA, and permission.
5. Download/export actions show retention and audit impact.
6. UI must never imply that masking changes source retention when it only changes display.
7. Support operators see tenant-sensitive media only during an active authorized grant.
8. Screenshots in training/demo environments use synthetic or consented media.

## 11. Accessibility Requirements

- WCAG 2.2 AA target.
- Full keyboard support for navigation, tables, filters, dialogs, timeline, and media controls.
- Visible focus with logical order.
- Minimum contrast 4.5:1 for normal text and 3:1 for large text/UI boundaries.
- Status and charts have text alternatives.
- Live updates use appropriate ARIA live priority; avoid repeated noisy announcements.
- Do not rely on hover for essential actions.
- Support 200% zoom without loss of primary workflows.
- Respect reduced motion; avoid flashing detection overlays.
- Captions/transcripts for instructional media.
- Date/time formatting respects locale and timezone while retaining precise UTC in technical details.

## 12. Content and Terminology

Preferred terms:

| Use | Avoid |
|---|---|
| Incident | Alarm everywhere; use alert only for notification/attention |
| Detection | Incident when only a model observation exists |
| Edge device | Box, machine, agent interchangeably |
| Pipeline | Model when referring to the entire rule workflow |
| Acknowledge | Accept |
| Resolve | Delete/complete when record remains |
| Organization / tenant | Customer/account without defined meaning |
| Developer Console | Admin dashboard when referring to platform operations |
| Customer CRM | Developer Console with hidden features |

Use sentence case. Buttons use verbs: `Add camera`, `Acknowledge incident`, `Publish version`, `Roll back deployment`.

## 13. Prototype Priorities

### Prototype A: Customer operational loop

1. Dashboard critical incident
2. Incident inbox
3. Incident evidence/detail
4. Acknowledge, assign, comment, resolve
5. Notification/escalation state

### Prototype B: Camera onboarding

1. Edge selection/enrollment
2. ONVIF/NVR discovery
3. Credential test
4. Stream and privacy selection
5. Pipeline observe-only assignment
6. Health verification

### Prototype C: AI control plane

1. Model version validation
2. Pipeline builder/test
3. Staging and canary
4. Production approval
5. Rollback and audit

### Prototype D: Privileged support

1. Tenant search
2. Grant request/approval
3. Active support banner
4. Evidence access
5. Expiry/revocation
6. Audit review

## 14. Research and Usability Tests

Test with tenant operators, tenant administrators, installers, reseller admins, platform operators, AI engineers, and auditors.

Critical tasks:

- Find and acknowledge the highest-risk unacknowledged incident.
- Explain why an incident was created.
- Add a camera without exposing credentials.
- Determine why a camera is offline.
- Create an escalation policy and predict recipients.
- Identify remaining license capacity.
- Register and promote a model with evidence.
- Roll back a pipeline deployment.
- Request and end tenant support access.
- Export a scoped audit report.

Target: ≥90% unassisted completion for critical production tasks; zero participant confusion about tenant, environment, privacy state, or whether a risky change has been committed.

## 15. Design Deliverables

- Validated sitemap for both applications
- Role/permission-to-navigation matrix
- Low-fidelity task flows
- High-fidelity desktop customer screens
- Responsive incident/mobile screens
- High-fidelity Developer Console screens
- Component library and semantic tokens
- Interactive prototypes for four priorities above
- Empty/loading/error/offline/forbidden/quota states
- Privacy/masking variants
- Accessibility annotations
- Redline/spec and API field mapping
- Usability findings and design change log

## 16. Design Acceptance Criteria

- Mode, tenant, environment, and support context are always unambiguous.
- Critical incident work requires no more than two navigation levels from dashboard.
- Camera onboarding is resumable and reports exact failed verification step.
- Pipeline production deployment cannot occur without version, target, test, impact, and rollback visibility.
- All sensitive media and privileged actions display privacy/audit implications.
- Core workflows pass keyboard-only and screen-reader QA.
- All backend states defined in Application Flows have a designed presentation.
- Design tokens and components are shared without merging the two application information architectures.

