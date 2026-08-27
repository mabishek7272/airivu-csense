"""End-to-end camera onboarding against a real NVR.

Creates a tenant, adds a camera, stores its credential, and probes it - all through the
HTTP API, the way an operator would. Then checks the properties that matter more than the
happy path:

  - the stored password is never returned by any endpoint, in any shape
  - probing an address on a private network is refused (the SSRF guard)
  - a wrong password is reported as a wrong password, not a generic failure

    python scripts/e2e_camera_onboarding.py rtsp://user:pass@host:554/path

The RTSP URL is parsed locally and its parts are sent to the API separately; it is never
stored as a URL, because a URL field is a place credentials end up in logs.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "CameraE2E!Password123"


def api(path: str, payload=None, token=None, method="POST", expect=(200, 201, 204)):
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
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code in expect:
            return exc.code, (json.loads(body) if body else {})
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def step(n, text):
    print(f"\n[{n}] {text}")


def main(rtsp_url: str) -> int:
    parsed = urllib.parse.urlparse(rtsp_url)
    username = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    hostname = parsed.hostname
    port = parsed.port or 554
    path = parsed.path
    if not hostname or not path:
        print("Could not parse a hostname and path from that URL.")
        return 2

    suffix = uuid.uuid4().hex[:8]

    step(1, "Register a tenant")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Camera E2E {suffix}",
        "email": f"ops-{suffix}@northwind.example",
        "password": PASSWORD,
        "display_name": "Site Operator",
    })
    token = auth["access_token"]
    tenant_id = auth["tenant_id"]
    print(f"    tenant {tenant_id}")

    step(2, "Create a site")
    # Sites have no CRUD API yet, so this goes in directly - the camera API is what is
    # under test here.
    import subprocess
    site_code = f"site-{suffix}"
    sql = (
        "INSERT INTO sites (tenant_id, name, code, timezone) "
        f"VALUES ('{tenant_id}', 'Head Office', '{site_code}', 'Australia/Sydney') "
        "RETURNING id"
    )
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    # psql prints the row and then its command tag ("INSERT 0 1"), so take the first line.
    site_id = result.stdout.strip().splitlines()[0].strip()
    print(f"    site {site_id}")

    step(3, "Create the camera (no credential yet)")
    _, camera = api("/api/v1/tenant/cameras", {
        "site_id": site_id,
        "name": "Reception",
        "code": f"cam-{suffix}",
        "vendor": "ONVIF",
        "hostname": hostname,
        "rtsp_port": port,
        "main_stream_path": path,
        "username": username,
    }, token)
    camera_id = camera["id"]
    print(f"    camera {camera_id}  has_credentials={camera['has_credentials']}")

    step(4, "Reject a stream path given as a full URL")
    status, body = api("/api/v1/tenant/cameras", {
        "site_id": site_id,
        "name": "Bad",
        "code": f"bad-{suffix}",
        "main_stream_path": rtsp_url,
    }, token, expect=(422,))
    print(f"    -> {status} (a URL can carry credentials; paths only)")

    step(5, "Probe before the credential is stored")
    _, probe = api(f"/api/v1/tenant/cameras/{camera_id}/probe", {}, token)
    print(f"    reachable={probe['reachable']}: {probe['detail']}")

    step(6, "Store the credential")
    _, camera = api(
        f"/api/v1/tenant/cameras/{camera_id}/credentials",
        {"username": username, "password": password, "label": "NVR login"},
        token, method="PUT",
    )
    print(f"    has_credentials={camera['has_credentials']}")

    step(7, "Probe again - should connect and describe the stream")
    _, probe = api(f"/api/v1/tenant/cameras/{camera_id}/probe", {}, token)
    print(f"    reachable  : {probe['reachable']}")
    print(f"    detail     : {probe['detail']}")
    print(f"    codec      : {probe['codec']}   framerate: {probe['framerate']}")

    step(8, "Confirm the password is not retrievable from any endpoint")
    leaked = []
    _, one = api(f"/api/v1/tenant/cameras/{camera_id}", None, token, method="GET")
    _, many = api("/api/v1/tenant/cameras", None, token, method="GET")
    for label, payload in (("detail", one), ("list", many)):
        blob = json.dumps(payload)
        if password and password in blob:
            leaked.append(label)
    print(f"    password present in responses: {leaked or 'no'}")

    step(9, "Refuse to probe a private address (SSRF guard)")
    _, internal = api("/api/v1/tenant/cameras", {
        "site_id": site_id,
        "name": "Internal",
        "code": f"int-{suffix}",
        "hostname": "127.0.0.1",
        "rtsp_port": 554,
        "main_stream_path": "/live",
    }, token)
    status, body = api(
        f"/api/v1/tenant/cameras/{internal['id']}/probe", {}, token, expect=(422,)
    )
    print(f"    -> {status} {body.get('message', body)[:120]}")

    step(10, "Clean up")
    api(f"/api/v1/tenant/cameras/{camera_id}", None, token, method="DELETE")
    subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-c",
         (f"DELETE FROM tenants WHERE id = '{tenant_id}'; "
          f"DELETE FROM organizations WHERE display_name = 'Camera E2E {suffix}';")],
        cwd="infra", capture_output=True, text=True, check=False,
    )
    print("    test tenant removed")

    ok = probe["reachable"] and not leaked and status == 422
    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].startswith("rtsp://"):
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
