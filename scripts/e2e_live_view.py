"""Proves the live-view media session service actually configures MediaMTX correctly.

Structural verification always runs: registers a throwaway tenant, creates a camera
pointed at the deployment's known real NVR host (DNS-resolvable and publicly reachable
regardless of whether real credentials are supplied - see [[nvr-h265-constraint]]), starts
an HLS and a WebRTC live session through the real Tenant API, and confirms MediaMTX itself
(via its own Control API) ended up with the right *kind* of path for each - a
`source`-based relay for HLS, a `runOnDemand`-based transcode for WebRTC, never the other
way round. Also exercises the auth webhook directly: a valid token is accepted, a token
presented against the wrong path is refused, and a bogus token is refused.

Real end-to-end playback verification is additional, and only runs if TEST_NVR_USERNAME/
TEST_NVR_PASSWORD are set in the environment - never hardcoded here, never printed, never
written anywhere. When set, this drives a real `ffprobe` (bundled in the `-ffmpeg` MediaMTX
image variant used specifically for the WebRTC transcode - see infra/docker-compose.yml)
against both resulting MediaMTX paths and confirms real video stream info comes back,
including that the webrtc path's on-demand transcode actually produced H.264 from the real
H.265 source.

    export TEST_NVR_USERNAME=...
    export TEST_NVR_PASSWORD=...
    python scripts/e2e_live_view.py
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
MEDIAMTX_CONTROL = "http://localhost:9997"
PASSWORD = "LiveViewE2E!Password123"

NVR_HOST = os.environ.get("TEST_NVR_HOST", "autotek-dorani-nvr.dyndns.org")
NVR_PORT = int(os.environ.get("TEST_NVR_PORT", "554"))
NVR_MAIN_PATH = os.environ.get("TEST_NVR_MAIN_PATH", "/unicast/c3/s0/live")
NVR_SUB_PATH = os.environ.get("TEST_NVR_SUB_PATH", "/unicast/c3/s1/live")
NVR_USERNAME = os.environ.get("TEST_NVR_USERNAME")
NVR_PASSWORD = os.environ.get("TEST_NVR_PASSWORD")


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
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def mediamtx_path(name):
    request = urllib.request.Request(f"{MEDIAMTX_CONTROL}/v3/config/paths/get/{name}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def ffprobe(path_name: str, token: str) -> dict | None:
    """Runs inside the mediamtx container, which bundles ffmpeg/ffprobe specifically for
    the webrtc on-demand transcode - reused here as the verification tool too, rather than
    installing a second copy anywhere else.

    The token has to travel as an RTSP URL query string (confirmed working against the
    real system, not assumed) - MediaMTX's auth webhook reads `token` from either the
    dedicated field or, for protocols with no such field of their own, the raw `query`
    string (see media.py's `_token_from_query`), and a raw RTSP read is exactly that case:
    without it, MediaMTX correctly refuses the read with 401, the same as it would for
    any other unauthenticated attempt.
    """
    url = f"rtsp://localhost:8554/{path_name}?token={token}"
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "mediamtx",
         "ffprobe", "-v", "quiet", "-rtsp_transport", "tcp", "-print_format", "json",
         "-show_streams", "-timeout", "15000000", url],
        cwd="infra", capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []
    real_nvr = bool(NVR_USERNAME and NVR_PASSWORD)

    step(1, "Register a tenant, create a site and a camera pointed at the real NVR host")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Live View E2E {suffix}",
        "email": f"ops-{suffix}@northwind.example",
        "password": PASSWORD,
        "display_name": "Live View Tester",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {
        "name": "Live View Site", "code": f"liveview-{suffix}", "timezone": "Asia/Kolkata",
    }, token)

    _, camera = api("/api/v1/tenant/cameras", {
        "site_id": site["id"], "name": "E2E Live Camera", "code": f"cam-{suffix}",
        "hostname": NVR_HOST, "rtsp_port": NVR_PORT,
        "main_stream_path": NVR_MAIN_PATH, "sub_stream_path": NVR_SUB_PATH,
    }, token)
    print(f"    tenant {tenant_id}, camera {camera['id']}")

    if real_nvr:
        api(f"/api/v1/tenant/cameras/{camera['id']}/credentials", {
            "username": NVR_USERNAME, "password": NVR_PASSWORD,
        }, token, method="PUT")

    step(2, "A camera that is not 'ready' refuses a live session")
    status, _ = api(f"/api/v1/tenant/cameras/{camera['id']}/live-session", {
        "protocol": "hls",
    }, token, expect=(409,))
    check(status == 409, "not-yet-probed camera refuses a live session (409)", failures)

    # Forces past the probe gate without needing this run to have real credentials -
    # camera_status.ready is the only thing start_live_session actually checks.
    api(f"/api/v1/tenant/cameras/{camera['id']}", {"status": "ready"}, token, method="PATCH")

    step(3, "HLS session: MediaMTX gets a source-based relay path, mainstream, no transcode")
    _, hls_session = api(f"/api/v1/tenant/cameras/{camera['id']}/live-session", {
        "protocol": "hls",
    }, token)
    check(hls_session["protocol"] == "hls", "session reports protocol=hls", failures)
    check("token" in hls_session and hls_session["token"], "a session token was issued", failures)

    mtx_status, mtx_hls = mediamtx_path(hls_session["path"])
    check(mtx_status == 200, "MediaMTX has the hls path configured", failures)
    check(bool(mtx_hls.get("source")), "hls path has a source (relay, no transcode)", failures)
    check(not mtx_hls.get("runOnDemand"), "hls path has no runOnDemand command", failures)
    check(NVR_MAIN_PATH in (mtx_hls.get("source") or ""), "hls path pulls the mainstream", failures)

    step(4, "WebRTC session: MediaMTX gets a runOnDemand transcode path, substream, no source")
    _, webrtc_session = api(f"/api/v1/tenant/cameras/{camera['id']}/live-session", {
        "protocol": "webrtc",
    }, token)
    check(webrtc_session["protocol"] == "webrtc", "session reports protocol=webrtc", failures)

    mtx_status, mtx_webrtc = mediamtx_path(webrtc_session["path"])
    check(mtx_status == 200, "MediaMTX has the webrtc path configured", failures)
    # MediaMTX's own GET reflects "no fixed source" as the literal sentinel "publisher"
    # (confirmed against the real response, not assumed), not an empty/absent value.
    check(
        mtx_webrtc.get("source") == "publisher",
        "webrtc path has no fixed source - awaits a publisher (the on-demand transcode)",
        failures,
    )
    check(bool(mtx_webrtc.get("runOnDemand")), "webrtc path has a runOnDemand command", failures)
    check(
        NVR_SUB_PATH in (mtx_webrtc.get("runOnDemand") or ""),
        "webrtc's transcode pulls the substream, not the mainstream (measured cost: "
        "~0.23 vs ~1.7 CPU cores/camera)",
        failures,
    )

    step(5, "The auth webhook accepts a valid session and refuses the wrong path/a bogus token")
    ok_status, _ = api("/api/v1/tenant/media/authenticate", {
        "token": hls_session["token"], "action": "read", "path": hls_session["path"],
    }, expect=(200,))
    check(ok_status == 200, "a valid token for its own path is accepted", failures)

    wrong_status, _ = api("/api/v1/tenant/media/authenticate", {
        "token": hls_session["token"], "action": "read", "path": webrtc_session["path"],
    }, expect=(401, 403))
    check(wrong_status == 403, "a valid token presented against a different path is refused", failures)

    bogus_status, _ = api("/api/v1/tenant/media/authenticate", {
        "token": "not-a-real-token", "action": "read", "path": hls_session["path"],
    }, expect=(401, 403))
    check(bogus_status == 401, "a bogus token is refused", failures)

    if real_nvr:
        step(6, "Real NVR: ffprobe actually finds video on both resulting MediaMTX paths")
        print("    (waiting for on-demand pull/transcode to start...)")
        time.sleep(3)

        hls_probe = ffprobe(hls_session["path"], hls_session["token"])
        hls_codec = (hls_probe or {}).get("streams", [{}])[0].get("codec_name") if hls_probe else None
        check(hls_probe is not None, "ffprobe found a real stream on the hls path", failures)
        check(hls_codec in ("hevc", "h264"), f"hls stream codec is {hls_codec!r} (real video)", failures)

        webrtc_probe = ffprobe(webrtc_session["path"], webrtc_session["token"])
        webrtc_codec = (
            (webrtc_probe or {}).get("streams", [{}])[0].get("codec_name") if webrtc_probe else None
        )
        check(webrtc_probe is not None, "ffprobe found a real stream on the webrtc (transcoded) path", failures)
        check(
            webrtc_codec == "h264",
            f"webrtc path is real H.264 (codec={webrtc_codec!r}) - the transcode actually ran, "
            "not just configured",
            failures,
        )
    else:
        print(
            "\n[6] Skipped: TEST_NVR_USERNAME/TEST_NVR_PASSWORD not set - structural "
            "verification only. Set both to prove this against the real NVR."
        )

    step(7, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Live View E2E {suffix}'")
    for path_name in (hls_session["path"], webrtc_session["path"]):
        try:
            request = urllib.request.Request(
                f"{MEDIAMTX_CONTROL}/v3/config/paths/delete/{path_name}", method="DELETE"
            )
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError:
            pass
    print("    test tenant and MediaMTX paths removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - live-view sessions configure MediaMTX correctly" + (
        " and play real video from the real NVR" if real_nvr else ""
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
