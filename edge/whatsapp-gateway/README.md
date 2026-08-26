# WhatsApp Gateway

The WhatsApp channel for CSense notifications. Vendored from
[Evolution Go](https://github.com/EvolutionAPI/evolution-go) by Evolution Foundation,
which implements the WhatsApp protocol via
[whatsmeow](https://github.com/tulir/whatsmeow).

## What is different from upstream

**Headless.** Upstream ships a browser console (`manager/dist`). It is not vendored here
and is not served. CSense drives the gateway entirely through its HTTP API and provides
its own instance-management UI in the Developer Console.

That is a licence decision as much as an architectural one. Evolution Go's licence
restricts modifying the LOGO and copyright *in their frontend components* (§1.a), while
their trademark policy requires that a **modified** UI remove their branding entirely
(TRADEMARKS §4.2). Not shipping their UI at all avoids the tension: we are not
distributing a modified version of their console, and our own console is our own work.

**Telemetry removed.** Upstream POSTs `{route, apiVersion, timestamp}` to
`log.evolution-api.com` on every API request. No message content or customer data is
included, but it is an outbound call per request from a service that sits inside a
customer's network — unacceptable for on-prem or air-gapped deployments, and not
disableable by configuration upstream. `pkg/telemetry/telemetry.go` is a no-op here.
Apache 2.0 permits the modification.

**Licence auto-activation.** Upstream can call `evolutionapi.com` at startup to activate
a licence (`EVOLUTION_OPERATOR_EMAIL`). Left unset, so the service does not phone home.

## Licence obligations we carry

Evolution Go is Apache 2.0 **plus two additional conditions**. Both are met:

| Obligation | Where |
|---|---|
| §1.b — display a notice to system administrators that Evolution Go is in use | Developer Console → Settings → Open source components |
| Apache §4(d) — preserve the `NOTICE` file | [NOTICE](NOTICE), kept verbatim |
| TRADEMARKS §4.2 — a modified/renamed distribution must not carry their marks | This service is named "WhatsApp Gateway"; their console is not shipped |

`LICENSE`, `NOTICE` and `TRADEMARKS.md` are kept in this directory unmodified. Do not
delete them — they are the terms this code is used under.

Upstream contact for licensing questions: `suporte@evofoundation.com.br`.

## The risk you should know about

whatsmeow speaks the WhatsApp **Web** protocol. Meta's terms prohibit unofficial clients,
and numbers using them are banned without warning and without a distinguishable error —
sends simply start failing.

For safety alerting that is a real operational risk, so the system is built to surface it
rather than absorb it:

- The provider interface (`csense_shared.notifications.whatsapp`) has a second
  implementation stub for Meta's official WhatsApp Business Cloud API. Switching is
  configuration, not a rewrite.
- A disconnected instance is classified as a **permanent** failure with the distinct code
  `whatsapp_instance_unavailable`, not a retryable one. Retrying a banned number for an
  hour would hide a dead integration behind a queue that never drains.
- Instance connection state is shown in the Developer Console, so "WhatsApp alerts are not
  going out" is visible before someone needs them.

See CLARIFICATIONS.md #22.

## Running it

The gateway is a Compose service (`whatsapp-gateway`) with its own PostgreSQL databases
for auth and session state. It is **not** exposed through Traefik: it holds live WhatsApp
sessions and its API key grants the ability to send as your number, so it is reachable
only from inside the Docker network.

Connecting a number:

1. Developer Console → Notifications → WhatsApp
2. Create an instance, scan the QR with WhatsApp → Linked devices
3. The console polls until the state reads `connected`

One instance is one WhatsApp number. Several can run side by side.
