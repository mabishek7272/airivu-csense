"""Task 4 of docs/superpowers/plans/2026-09-08-pipeline-execution-runtime.md: proves the
whole point of the plan against the live stack, with the real `pipeline-runtime` container
actually running (not imported as a module, not mocked at any layer) -

    a camera is pointed at a real RTSP stream containing something `yolov8n-general`
    genuinely detects, a real tenant assigns a real published pipeline version to it, and
    **nobody ever posts a detection by hand** - a real incident, with real evidence, has to
    show up on its own.

**The test video is a real photograph, not a synthetic pattern.** `yolov8n-general` is a
general COCO-class detector; a colour-bar test pattern (the fixture
`backend/tests/test_frame_grab.py`/`test_pipeline_runtime_service.py` already use to prove
the *decode* path) has nothing in it for a detector to find. `SAMPLE_IMAGE` is the exact
photograph `scripts/e2e_detection_to_incident.py` already proved `yolov8n-general` fires on
(person + bus, at real, measured confidence) - looped into a real H.264 stream and pushed
into a real, throwaway MediaMTX server with `ffmpeg`. The stream is real RTSP end to end
(real GOP, real TCP transport, real decode); only the *content* is a still photograph
rather than motion, which is irrelevant to what is being proven - that a real frame, really
decoded and really run through the model, produces a real incident with nobody in the loop.

**Reaching that stream from inside the live stack, past the SSRF allowlist, needs the same
provisioned-tunnel trick Task 1 established** (see this plan's own Task 1 annotation, and
`scripts/e2e_vpn_camera.py`): the throwaway MediaMTX container joins the *same* Docker
network `docker compose` already created (not an isolated one, unlike
`test_pipeline_runtime_service.py`'s own container-decode test, which never has to clear
the allowlist because it calls `grab_frame` directly) so the real `pipeline-runtime`
container can reach it by IP, and that IP is declared as a real camera's `edge_device`
tunnel (`connection_mode='vpn'`, `vpn_address` = the container's own address) so
`resolve_camera_endpoint` actually permits the connection instead of refusing it as a
private address.

**The pipeline registry has no HTTP surface reachable from the host** (`admin-api`
publishes no port and sits behind no Traefik route - `scripts/e2e_pipeline_registry.py`
drives it through the Developer Console UI instead). Authoring a pipeline and version is
not this task's subject the way executing one is, so - matching this codebase's own
"seeded directly, camera API is what's under test" convention
(`scripts/e2e_detection_to_incident.py` step 2) - the pipeline and its published version are
inserted directly, exactly the shape `POST /pipeline-versions/{id}/publish` would have left
behind. Everything this task actually exists to prove (the camera, the tenant, the
assignment, the rule, the running detection loop) goes through the real HTTP APIs.

**Ordering deviates slightly from the task's own checklist, deliberately.** The checklist
lists "confirm no further incidents after revoke" (step 4) before "point a camera at an
unreachable address" (step 5). This script runs the unreachable-camera check *while the
real camera's assignment is still active*, then revokes - so the isolation property (one
camera's every-cycle failure never affects another camera's success) is actually observed
under real concurrent load, not just asserted in sequence. Both checklist properties are
still fully proven; this is a reordering of the proof, not a reduction of it.

Run from the repo root with the stack up (`docker compose --env-file .env -f
infra/docker-compose.yml up -d`, `pipeline-runtime` included):

    python scripts/e2e_pipeline_execution.py
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "PipelineExecE2E!Password123"
COMPOSE = ["docker", "compose", "--env-file", "../.env"]
INFRA_DIR = "infra"

MEDIAMTX_IMAGE = "bluenviron/mediamtx:1.20.1-ffmpeg"  # same pinned tag as infra/docker-compose.yml
STREAM_PATH_NAME = "e2epipeline"

# The exact photograph scripts/e2e_detection_to_incident.py already proved yolov8n-general
# detects for real (person + bus, at real measured confidence) - reused here rather than
# sourcing new test media, per this task's own instruction to check for one first.
SAMPLE_IMAGE_URL = "https://ultralytics.com/images/bus.jpg"

MODEL_NAME = "yolov8n-general"

# A fast, test-only sample rate (CLAUDE.md's own production recommendation is ~0.5fps
# mainstream-keyframe-only) - carried on the pipeline version's own resource_profile, not a
# tenant override, so no allowed_overrides_schema entry is needed for it.
TEST_SAMPLE_FPS = 2.0
TEST_CONFIDENCE = 0.4

# TEST-NET-3 (RFC 5737) - never routed on the real internet, and refused by
# csense_shared.security.outbound as a private address with no tunnel provisioned. The
# same address class Task 3's own real-container verification used for exactly this
# reason (see its docker logs: "outbound_address_blocked" / "camera_endpoint_blocked").
UNREACHABLE_HOSTNAME = "203.0.113.55"

DISCOVERY_POLL_SECONDS = 5.0  # pipeline_runtime_discovery_poll_seconds default


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": "app.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code in expect:
            return exc.code, (json.loads(body) if body else {})
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:600]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        [*COMPOSE, "exec", "-T", "postgres", "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd=INFRA_DIR, capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def docker(*args: str, input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, input=input_bytes, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:800]}"
        )
    return result


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


# --- Compose plumbing, matching scripts/e2e_edge_spool.py's own established pattern -----


def compose_project_and_network() -> tuple[str, str]:
    container_id = subprocess.run(
        [*COMPOSE, "ps", "-q", "tenant-api"], cwd=INFRA_DIR, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not container_id:
        raise RuntimeError("tenant-api is not running - start the stack first (docker compose up -d).")
    project = docker(
        "inspect", "-f", '{{ index .Config.Labels "com.docker.compose.project" }}', container_id
    ).stdout.decode().strip()
    networks = json.loads(docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", container_id).stdout)
    network = next(iter(networks))
    return project, network


def ensure_pipeline_runtime_running() -> str:
    """Returns the running pipeline-runtime container's id. Starts it if the compose stack
    doesn't already have it up - this task's whole point requires it running as a real
    container, not imported as a module."""
    container_id = subprocess.run(
        [*COMPOSE, "ps", "-q", "pipeline-runtime"], cwd=INFRA_DIR, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not container_id:
        subprocess.run([*COMPOSE, "up", "-d", "pipeline-runtime"], cwd=INFRA_DIR, check=True)
        container_id = subprocess.run(
            [*COMPOSE, "ps", "-q", "pipeline-runtime"], cwd=INFRA_DIR, capture_output=True, text=True, check=True,
        ).stdout.strip()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        running = docker("inspect", "-f", "{{.State.Running}}", container_id).stdout.decode().strip()
        if running == "true":
            return container_id
        time.sleep(1.0)
    raise RuntimeError("pipeline-runtime container never reached a running state")


def container_running(container_id: str) -> bool:
    result = docker("inspect", "-f", "{{.State.Running}}", container_id, check=False)
    return result.returncode == 0 and result.stdout.decode().strip() == "true"


def container_logs(container_id: str) -> str:
    result = docker("logs", container_id, check=False)
    return result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")


def container_ip(container_id: str, network: str) -> str:
    data = json.loads(docker("inspect", container_id).stdout)
    return data[0]["NetworkSettings"]["Networks"][network]["IPAddress"]


# --- Real, throwaway MediaMTX source, joined to the stack's own network -----------------


def _wait_for_tcp(host: str, port: int, *, timeout: float) -> None:
    import socket

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"{host}:{port} never accepted a connection: {last_error}")


def _wait_for_path_ready(api_port: int, path_name: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{api_port}/v3/paths/get/{path_name}", timeout=1.0
            ) as resp:
                if json.loads(resp.read()).get("ready"):
                    return
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"mediamtx path '{path_name}' never became ready")


def _free_tcp_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def download_sample_image(dest_path: pathlib.Path) -> None:
    # curl, not urllib: the real URL 308-redirects (ultralytics.com -> www.ultralytics.com
    # -> a CDN edge), and older stdlib urllib does not follow 308s. curl -L handles any
    # redirect chain without this script having to hand-roll one.
    subprocess.run(
        ["curl", "-sSL", "--max-time", "30", "-o", str(dest_path), SAMPLE_IMAGE_URL], check=True,
    )
    if dest_path.stat().st_size < 1000:
        raise RuntimeError(f"downloaded sample image is suspiciously small: {dest_path.stat().st_size} bytes")


def start_throwaway_mediamtx(network: str, suffix: str) -> tuple[str, int, int, pathlib.Path]:
    """Starts a real, throwaway MediaMTX server joined to the *same* Docker network the
    live stack's own containers are on (unlike backend/tests/test_frame_grab.py's own
    fixture, which only needs the host to reach it) - so the real, already-running
    pipeline-runtime container can dial it by IP. Returns (container_id, host_rtsp_port,
    host_api_port, config_dir); the host ports are only for this script's own publish/
    readiness-check use, never what pipeline-runtime dials. `config_dir` is returned so
    the caller can remove it in cleanup - it's bind-mounted read-only into the container
    (Linux keeps the mount valid via the held inode even after the source path is
    unlinked, so removing it here after the container is already up is safe), but nothing
    was deleting it before, leaking one throwaway temp dir per run.
    """
    container_name = f"csense-pipeline-exec-e2e-mtx-{suffix}"
    rtsp_port = _free_tcp_port()
    api_port = _free_tcp_port()

    config_dir = pathlib.Path(tempfile.mkdtemp(prefix="csense-pipeline-exec-e2e-"))
    config_path = config_dir / "mediamtx.yml"
    config_path.write_text(
        "logLevel: error\n"
        "rtspAddress: :8554\n"
        "api: yes\n"
        "apiAddress: :9997\n"
        "authInternalUsers:\n"
        "  - user: any\n"
        "    pass:\n"
        "    ips: []\n"
        "    permissions:\n"
        "      - action: api\n"
        "      - action: publish\n"
        "      - action: read\n"
        "      - action: playback\n"
        "paths:\n"
        "  all_others:\n"
    )
    docker(
        "run", "-d", "--name", container_name, "--network", network,
        "-p", f"127.0.0.1:{rtsp_port}:8554",
        "-p", f"127.0.0.1:{api_port}:9997",
        "-v", f"{config_path}:/mediamtx.yml:ro",
        MEDIAMTX_IMAGE,
    )
    _wait_for_tcp("127.0.0.1", rtsp_port, timeout=15.0)
    return container_name, rtsp_port, api_port, config_dir


def publish_sample_image_loop(image_path: pathlib.Path, rtsp_port: int, api_port: int) -> subprocess.Popen:
    """Loops the real sample photograph into a real H.264 RTSP stream via a real ffmpeg
    process - the same 'ffmpeg re-streams real content into a real RTSP server' shape
    backend/tests/test_frame_grab.py already established, just with a real photograph
    instead of a synthetic test pattern (see this module's own docstring for why that
    matters). A 1s GOP (-g 15 -keyint_min 15 at 15fps) matches CLAUDE.md's own "shortening
    it to 1s GOP" recommendation and backend/tests/test_frame_grab.py's own finding that a
    longer GOP makes a fresh RTSP read coin-flip flaky against a read timeout.
    """
    publisher = subprocess.Popen(
        [
            "ffmpeg", "-y", "-re", "-loop", "1", "-i", str(image_path),
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
            "-pix_fmt", "yuv420p", "-r", "15", "-g", "15", "-keyint_min", "15",
            "-t", "1800",
            "-f", "rtsp", "-rtsp_transport", "tcp",
            f"rtsp://127.0.0.1:{rtsp_port}/{STREAM_PATH_NAME}",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _wait_for_path_ready(api_port, STREAM_PATH_NAME, timeout=15.0)
    return publisher


# --- Pipeline registry: seeded directly (no HTTP surface reachable from the host) -------


def _definition_sha256(definition_json: dict) -> str:
    # Mirrors backend/admin_api/app/api/pipelines.py's own _definition_sha256 exactly -
    # this script inserts a pipeline_versions row directly (see this module's own
    # docstring for why), so it must produce the identical digest a real
    # POST /pipeline-versions would have, satisfying the same ck_pipeline_version_sha256_format
    # and uq_pipeline_version_definition_sha256 constraints a real publish would.
    canonical = json.dumps(definition_json, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_published_pipeline_version(suffix: str) -> str:
    pipeline_code = f"e2e-pipeline-exec-{suffix}"
    pipeline_id = psql(
        "INSERT INTO pipelines (code, name, use_case) VALUES "
        f"('{pipeline_code}', 'E2E Pipeline Execution', 'person.detection') RETURNING id"
    )
    definition = {"stages": [{"type": "infer", "model_name": MODEL_NAME, "min_model_state": "production"}]}
    definition_sha256 = _definition_sha256(definition)
    resource_profile = {"sample_fps": TEST_SAMPLE_FPS, "confidence": TEST_CONFIDENCE}
    version_id = psql(
        "INSERT INTO pipeline_versions "
        "(pipeline_id, version_number, definition_json, definition_sha256, "
        " allowed_overrides_schema, runtime_target, resource_profile, state) "
        f"VALUES ('{pipeline_id}', 1, '{json.dumps(definition)}'::jsonb, "
        f"'{definition_sha256}', '{{}}'::jsonb, 'cloud', "
        f"'{json.dumps(resource_profile)}'::jsonb, 'published') RETURNING id"
    )
    return version_id


# --- Polling helpers ---------------------------------------------------------------------


def wait_for_incident(token: str, camera_id: str, *, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, page = api(f"/api/v1/tenant/incidents?camera_id={camera_id}", None, token, method="GET")
        if page["items"]:
            return page["items"][0]
        time.sleep(2.0)
    return None


def incident_detection_count(token: str, incident_id: str) -> int:
    _, detail = api(f"/api/v1/tenant/incidents/{incident_id}", None, token, method="GET")
    return detail["detection_count"]


def count_log_occurrences(logs: str, *needles: str) -> int:
    return sum(1 for line in logs.splitlines() if all(n in line for n in needles))


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []
    organization_name = f"Pipeline Exec E2E {suffix}"
    email = f"owner-{suffix}@northwind.example"

    tenant_id = None
    mediamtx_container = None
    publisher: subprocess.Popen | None = None
    pipeline_version_id = None
    image_dir: pathlib.Path | None = None
    mediamtx_config_dir: pathlib.Path | None = None

    try:
        step(0, "Confirm the real pipeline-runtime container is up (not imported as a module)")
        project, network = compose_project_and_network()
        runtime_container = ensure_pipeline_runtime_running()
        print(f"    project={project} network={network} pipeline-runtime={runtime_container[:12]}")
        check(container_running(runtime_container), "pipeline-runtime container is running", failures)

        step(1, "Publish a real photograph yolov8n-general genuinely detects into a real RTSP stream")
        image_dir = pathlib.Path(tempfile.mkdtemp(prefix="csense-pipeline-exec-e2e-"))
        image_path = image_dir / "bus.jpg"
        download_sample_image(image_path)
        print(f"    downloaded sample image: {image_path.stat().st_size:,} bytes")

        mediamtx_container, rtsp_port, api_port, mediamtx_config_dir = start_throwaway_mediamtx(network, suffix)
        publisher = publish_sample_image_loop(image_path, rtsp_port, api_port)
        mediamtx_ip = container_ip(mediamtx_container, network)
        print(f"    throwaway mediamtx {mediamtx_container} joined to '{network}' at {mediamtx_ip}")

        step(2, "Real tenant, site, edge device provisioned as this stream's tunnel, and a real camera")
        _, auth = api("/api/v1/auth/register", {
            "organization_name": organization_name, "email": email,
            "password": PASSWORD, "display_name": "Owner",
        })
        token, tenant_id = auth["access_token"], auth["tenant_id"]
        print(f"    tenant {tenant_id}")

        _, site = api("/api/v1/tenant/sites", {
            "name": "E2E Site", "code": f"site-{suffix}", "timezone": "UTC",
        }, token, expect=(201,))
        site_id = site["id"]

        _, device = api("/api/v1/tenant/edge/devices", {
            "name": "E2E tunnel gateway", "site_id": site_id,
            "device_type": "pc_linux", "role": "gateway",
        }, token, expect=(201,))
        # Real DB rows standing in for a real WireGuard allocation - the same "provisioned
        # directly, no allocation API exists" shape scripts/e2e_vpn_camera.py already
        # established - but the address itself is a real, live, currently-reachable
        # container on the stack's own network, not a documentation-range placeholder.
        psql(f"UPDATE edge_devices SET vpn_address = '{mediamtx_ip}' WHERE id = '{device['id']}'")

        _, camera = api("/api/v1/tenant/cameras", {
            "site_id": site_id, "name": "E2E Camera", "code": f"cam-a-{suffix}",
            "hostname": mediamtx_ip, "rtsp_port": 8554, "main_stream_path": f"/{STREAM_PATH_NAME}",
        }, token, expect=(201,))
        camera_id = camera["id"]
        psql(
            f"UPDATE cameras SET connection_mode = 'vpn', edge_device_id = '{device['id']}' "
            f"WHERE id = '{camera_id}'"
        )
        print(f"    camera {camera_id} -> {mediamtx_ip}:8554/{STREAM_PATH_NAME} (vpn, tunnelled)")

        step(3, "A real published pipeline version (yolov8n-general, runtime_target=cloud)")
        pipeline_version_id = create_published_pipeline_version(suffix)
        print(f"    pipeline_version {pipeline_version_id} (sample_fps={TEST_SAMPLE_FPS}, "
              f"confidence={TEST_CONFIDENCE})")

        step(4, "A real active assignment, and a real rule that will actually fire on 'person'")
        status, assignment = api(f"/api/v1/tenant/cameras/{camera_id}/pipeline-assignments", {
            "pipeline_version_id": pipeline_version_id,
        }, token, expect=(201,))
        check(status == 201, "assignment created", failures)
        assignment_id = assignment["id"]

        _, rule = api("/api/v1/tenant/rules", {
            "site_id": site_id, "camera_id": camera_id,
            "name": "E2E person rule", "type_code": "zone.intrusion",
            "alertable_classes": ["person"], "min_confidence": TEST_CONFIDENCE, "severity": "high",
        }, token, expect=(201,))
        print(f"    assignment {assignment_id}, rule {rule['id']}")

        step(5, "Nobody posts a detection by hand: poll GET /incidents for one to appear on its own")
        incident_wait_timeout = DISCOVERY_POLL_SECONDS + 90.0
        incident = wait_for_incident(token, camera_id, timeout=incident_wait_timeout)
        check(
            incident is not None,
            f"a real incident appeared on its own within {incident_wait_timeout:.0f}s "
            "(camera -> RTSP frame -> real inference -> real ingest -> real incident)",
            failures,
        )
        if incident is None:
            print(f"    logs tail:\n{container_logs(runtime_container)[-3000:]}")
        else:
            print(f"    incident #{incident['incident_number']}  {incident['title']}  "
                  f"severity={incident['severity']}  detections={incident['detection_count']}")

            _, evidence_page = api(
                f"/api/v1/tenant/detections?camera_id={camera_id}&with_evidence_only=true&limit=5",
                None, token, method="GET",
            )
            check(
                bool(evidence_page["items"]),
                "real evidence (an annotated snapshot) is attached to a detection on this camera",
                failures,
            )
            if evidence_page["items"]:
                evidenced = evidence_page["items"][0]
                check(
                    any(e["url"] for e in evidenced["evidence"]),
                    "the evidence entry carries a real, retrievable URL", failures,
                )
                check(
                    evidenced.get("incident_number") == incident["incident_number"],
                    "the evidenced detection is linked to the same incident", failures,
                )

            step(6, "A second camera pointed at an unreachable address, run concurrently with camera A")
            _, unreachable_camera = api("/api/v1/tenant/cameras", {
                "site_id": site_id, "name": "E2E Unreachable Camera", "code": f"cam-b-{suffix}",
                "hostname": UNREACHABLE_HOSTNAME, "rtsp_port": 554, "main_stream_path": "/blocked",
            }, token, expect=(201,))
            unreachable_camera_id = unreachable_camera["id"]
            _, unreachable_assignment = api(
                f"/api/v1/tenant/cameras/{unreachable_camera_id}/pipeline-assignments",
                {"pipeline_version_id": pipeline_version_id}, token, expect=(201,),
            )
            print(f"    camera {unreachable_camera_id} -> {UNREACHABLE_HOSTNAME} (never routable)")

            detection_count_before_isolation_window = incident_detection_count(token, incident["id"])
            wait_window = DISCOVERY_POLL_SECONDS + 15.0
            print(f"    waiting {wait_window:.0f}s for both cameras' tasks to run concurrently...")
            time.sleep(wait_window)

            logs = container_logs(runtime_container)
            blocked_occurrences = count_log_occurrences(
                logs, "camera_endpoint_blocked", unreachable_camera_id
            )
            check(
                blocked_occurrences >= 2,
                f"the service logged the unreachable camera as blocked and kept retrying "
                f"({blocked_occurrences} occurrences, not just once)", failures,
            )
            check(
                container_running(runtime_container),
                "pipeline-runtime is still running after a camera failed repeatedly", failures,
            )

            detection_count_after_isolation_window = incident_detection_count(token, incident["id"])
            check(
                detection_count_after_isolation_window > detection_count_before_isolation_window,
                f"camera A's own task kept working the whole time camera B failed repeatedly "
                f"(detection_count {detection_count_before_isolation_window} -> "
                f"{detection_count_after_isolation_window})", failures,
            )

            step(7, "Revoke camera A's assignment; confirm no *further* incidents/detections appear")
            api(f"/api/v1/tenant/pipeline-assignments/{assignment_id}/revoke", {}, token, expect=(200,))
            # Give the discovery loop a full interval (plus margin) to notice the revoke and
            # actually cancel the running task - not just stop mattering.
            settle_seconds = DISCOVERY_POLL_SECONDS + 8.0
            time.sleep(settle_seconds)
            count_right_after_revoke = incident_detection_count(token, incident["id"])

            logs_after_revoke = container_logs(runtime_container)
            check(
                count_log_occurrences(logs_after_revoke, "camera_task_stopped", camera_id) >= 1,
                "the service's own logs confirm camera A's task actually stopped (not just "
                "stopped mattering)", failures,
            )

            further_wait_seconds = DISCOVERY_POLL_SECONDS + 10.0
            time.sleep(further_wait_seconds)
            count_later = incident_detection_count(token, incident["id"])
            check(
                count_later == count_right_after_revoke,
                f"no further detections landed after the revoke settled "
                f"({count_right_after_revoke} -> {count_later} over a further "
                f"{further_wait_seconds:.0f}s)", failures,
            )

            step(8, "Clean up the unreachable camera's assignment too")
            api(
                f"/api/v1/tenant/pipeline-assignments/{unreachable_assignment['id']}/revoke",
                {}, token, expect=(200,),
            )

    finally:
        step("cleanup", "Tear down every real resource this run created")
        if publisher is not None:
            publisher.terminate()
            try:
                publisher.wait(timeout=5)
            except subprocess.TimeoutExpired:
                publisher.kill()
            print("    stopped the ffmpeg publisher")
        if mediamtx_container is not None:
            docker("rm", "-f", mediamtx_container, check=False)
            print(f"    removed throwaway mediamtx container {mediamtx_container}")
        # Both temp dirs (the downloaded sample image, the mediamtx.yml bind-mount source)
        # were leaked on every run - a real, if small, disk-litter bug found by code
        # review, not present in any exception path only: shutil.rmtree(..., ignore_errors=True)
        # so a missing/already-cleaned dir never masks a real failure being reported above.
        if image_dir is not None:
            shutil.rmtree(image_dir, ignore_errors=True)
            print(f"    removed temp image dir {image_dir}")
        if mediamtx_config_dir is not None:
            shutil.rmtree(mediamtx_config_dir, ignore_errors=True)
            print(f"    removed temp mediamtx config dir {mediamtx_config_dir}")
        if tenant_id:
            psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
            psql(f"DELETE FROM organizations WHERE display_name = '{organization_name}'")
            psql(f"DELETE FROM users WHERE email_normalized = CAST('{email}' AS citext)")
            print("    removed the test tenant, organization and user (cascades cameras, "
                  "sites, edge devices, rules, assignments, detections, incidents)")
        if pipeline_version_id:
            # Platform-global, not owned by the tenant - not cascaded by the tenant delete
            # above. Mirrors scripts/e2e_pipeline_registry.py's own cleanup order: any
            # remaining assignment first (FK), then the version, then the pipeline.
            psql(f"DELETE FROM pipeline_assignments WHERE pipeline_version_id = '{pipeline_version_id}'")
            pipeline_id = psql(
                f"SELECT pipeline_id FROM pipeline_versions WHERE id = '{pipeline_version_id}'"
            )
            psql(f"DELETE FROM pipeline_versions WHERE id = '{pipeline_version_id}'")
            if pipeline_id:
                psql(f"DELETE FROM pipelines WHERE id = '{pipeline_id}'")
            print("    removed the throwaway pipeline and its version")

    return _finish(failures)


def _finish(failures: list[str]) -> int:
    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        "PASS - a real camera, on its own, produced a real incident with real evidence "
        "through the running pipeline-runtime service; a revoked assignment's task actually "
        "stopped; an unreachable camera was logged and retried without affecting another "
        "camera's own task or crashing the service."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
