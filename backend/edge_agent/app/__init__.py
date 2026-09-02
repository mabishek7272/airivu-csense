"""The CSense edge agent — what runs on the box in the customer's building.

Its job is enrolment, heartbeat, and the event pipeline: accept detections, deliver them
to the Tenant API, spool them locally when the uplink is down, and drain that spool when
it comes back. FLOW-13 in `docs/03_APPLICATION_FLOWS.md` is the behaviour it implements.

**This agent does not do inference, and does not pretend to.** It has no model, no
decoder, and no camera connection. Detections arrive from a *source* — in practice a
co-located inference process (the existing `ai-runtime`, or anything else) handing events
to a small local HTTP listener the agent owns. Running models on ARM edge hardware is a
larger, separate piece of work and belongs with the Phase 4 AI programme; building a
`detector` module here that produced nothing real would make the deployment look capable
of something it is not, which is the worst possible thing for a safety product to do.

**It deliberately does not depend on `csense_shared`.** That package declares sqlalchemy,
asyncpg, redis, minio, argon2-cffi and pyjwt[crypto] — several needing a C toolchain on
arm64, and none of them needed by a process whose entire interaction with the platform is
HTTP. The agent's dependency set is `httpx` + `cryptography` + the standard library
(`sqlite3` included), all of which publish prebuilt `aarch64` wheels, so the image builds
on a Pi-class target without a compiler. Anything shared with the server is shared by
being small enough to restate, not by importing the server's world onto a device.

**Its spool key is its own** — see `crypto.py`. The platform KEK never comes here.

At-least-once delivery plus server-side deduplication is what makes this safe: a spooled
row is deleted only once the server has acknowledged it, and the server already enforces a
unique `(tenant_id, source_event_id)`, so a replayed batch is a no-op. Every part of this
package has to preserve that property.
"""
