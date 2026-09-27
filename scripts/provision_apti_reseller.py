#!/usr/bin/env python3
"""Provisions a real Apti reseller organization + branding + one test tenant with a site
and camera, driving the platform's real HTTP APIs end-to-end (never raw SQL - every step
below already has a real, tested endpoint). Prints every generated credential/id at the
end, matching seed_demo_tenant.py's own convention.

Usage:
  python scripts/provision_apti_reseller.py \\
    --admin-api-base https://<admin-api-host> \\
    --tenant-api-base https://<tenant-api-host> \\
    --platform-admin-email admin@platform.dev \\
    --platform-admin-password <the real seeded password> \\
    --camera-site-name "Apti HQ" --camera-site-code apti-hq \\
    --camera-name "Front door" --camera-code front-door \\
    --camera-hostname <real reachable hostname/ip> --camera-username <real username> \\
    --camera-password <real password> [--camera-rtsp-port 554]

The --camera-* arguments describe a real Apti camera the operator points this at. This
script never fabricates a working camera - it registers whatever hostname/credentials it
is given. `POST /api/v1/tenant/cameras` only validates and stores connection details (see
`backend/tenant_api/app/api/cameras.py::create_camera`); it does not attempt to reach the
camera at creation time. Reachability is only ever checked by the separate, explicit
`POST /cameras/{id}/probe` endpoint, which this script deliberately never calls - probing
is a privileged action a human should trigger once real hardware is in place, not
something a provisioning script does on someone's behalf.

## Two out-of-band steps this script cannot avoid, and how it handles them

Two points in this flow hand control to a person outside this process: accepting an
emailed invitation, and reading a one-time sign-in code from an inbox. Both are handled
the same way:

  - In a **local/dev deployment with no real email provider configured** (empty
    `RESEND_API_KEY`), the platform's own invite/login endpoints fall back to returning
    the invitation link or OTP code directly in the HTTP response rather than only
    emailing it (see `organizations.py`/`reseller.py`'s own `invitation_link` field, and
    `auth.py`'s `_mfa_required_with_dev_code`). This script follows that fallback
    automatically end-to-end in a single run - it is not reading anyone's real inbox, it
    is using the same dev convenience the platform already built for exactly this.
  - In a **real deployment with email actually configured**, those values are only ever
    emailed to the real owner, and this script cannot see them. It stops cleanly at that
    point, prints what to do (check the inbox, accept the invitation, note the OTP), and
    accepts `--resume-*` arguments so a second invocation can continue from exactly that
    point without repeating anything already done.

## Why there is no separate "login as Apti owner" step

The plan this script was written from assumed a login call (with `client="web"`) after
accepting the owner's invitation, to obtain a token for creating the child tenant. That
assumption predates this repo's login-time MFA (`/auth/login` -> `/auth/login/mfa-verify`,
added earlier in this same plan) and no longer holds - MFA now gates *every* `/auth/login`
call regardless of `client`, including a first-time login with no enrolled TOTP factor
(email OTP), so a bare `/auth/login` call can no longer return a usable token by itself.

`POST /api/v1/auth/accept-invitation` was and remains the one auth endpoint that returns a
real, usable `AuthResponse` directly, without an MFA step - correct on its own terms: MFA
protects a sign-*in*, and accepting a single-use invitation token that was only ever valid
for this one email address already *is* the equivalent proof of control the platform asks
for elsewhere. So this script uses the access token `accept-invitation` itself returns,
for both the Apti owner and the child tenant's owner, rather than logging in separately
afterwards.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import uuid

import httpx


def _post(client: httpx.Client, path: str, body: dict, *, token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.post(path, json=body, headers=headers)
    if response.status_code >= 400:
        print(f"ERROR: POST {path} -> {response.status_code}: {response.text}", file=sys.stderr)
        response.raise_for_status()
    return response.json() if response.content else {}


def _put(client: httpx.Client, path: str, body: dict, *, token: str) -> dict:
    response = client.put(path, json=body, headers={"Authorization": f"Bearer {token}"})
    if response.status_code >= 400:
        print(f"ERROR: PUT {path} -> {response.status_code}: {response.text}", file=sys.stderr)
        response.raise_for_status()
    return response.json() if response.content else {}


def _invitation_token_from_link(link: str, *, label: str) -> str:
    parsed = urllib.parse.urlparse(link)
    token = urllib.parse.parse_qs(parsed.query).get("token", [None])[0]
    if token is None:
        print(f"Could not parse an invitation token out of {label}'s link: {link}", file=sys.stderr)
        sys.exit(1)
    return token


def _accept_invitation(tenant: httpx.Client, *, token: str, password: str, label: str) -> str:
    """Accepts an invitation and returns the resulting access token directly -
    `POST /auth/accept-invitation` issues real tokens itself (see module docstring for why
    that means no separate login/MFA step is needed here)."""
    accepted = _post(tenant, "/api/v1/auth/accept-invitation", {"token": token, "password": password})
    print(f"   {label} accepted their invitation and is now active (tenant_id={accepted.get('tenant_id')}).")
    return accepted["access_token"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--admin-api-base", required=True)
    parser.add_argument("--tenant-api-base", required=True)
    parser.add_argument("--platform-admin-email", required=True)
    parser.add_argument("--platform-admin-password", required=True)

    parser.add_argument("--apti-owner-email", default="owner@apti.example")
    parser.add_argument("--apti-owner-display-name", default="Apti Owner")
    parser.add_argument("--customer-org-name", default="Apti Test Customer")
    parser.add_argument("--customer-owner-email", default="owner@apti-test-customer.example")
    parser.add_argument("--customer-owner-display-name", default="Test Customer Owner")

    parser.add_argument("--branding-slug", default="apti")
    parser.add_argument("--branding-display-name", default="Apti")
    parser.add_argument("--branding-email-from-name", default="Apti Security")
    parser.add_argument("--branding-color-primary", default="#5d5294")
    parser.add_argument("--branding-color-accent", default="#5c5783")
    parser.add_argument("--branding-color-canvas", default="#f3f5fe")

    parser.add_argument("--camera-site-name", default="Apti HQ")
    parser.add_argument("--camera-site-code", default="apti-hq")
    parser.add_argument("--camera-site-timezone", default="UTC")
    parser.add_argument("--camera-name", default="Test camera")
    parser.add_argument("--camera-code", default="test-camera")
    parser.add_argument("--camera-hostname", required=True)
    parser.add_argument("--camera-username", required=True)
    parser.add_argument("--camera-password", required=True)
    parser.add_argument("--camera-rtsp-port", type=int, default=554)
    parser.add_argument("--camera-main-stream-path", default=None)
    parser.add_argument("--camera-sub-stream-path", default=None)

    # Resume arguments: used only when a real email provider actually delivered an
    # invitation (so this process never saw the token/link) and a human has since
    # accepted it out of band. Each lets the script skip the step it can no longer see
    # and continue from the access token that step would otherwise have produced.
    parser.add_argument(
        "--resume-apti-owner-token", default=None,
        help="Skip org creation + owner-invitation-accept; use this already-issued Apti owner access token instead.",
    )
    parser.add_argument(
        "--resume-apti-organization-id", default=None,
        help="Required alongside --resume-apti-owner-token, since branding is set via the Admin API by org id, "
             "not something derivable from the owner's own token.",
    )
    parser.add_argument(
        "--resume-customer-owner-token", default=None,
        help="Skip child-tenant creation + customer-owner-invitation-accept; use this already-issued access token "
             "instead, to create only the site/camera under an already-provisioned test customer tenant.",
    )
    args = parser.parse_args()

    admin = httpx.Client(base_url=args.admin_api_base, timeout=30.0)
    tenant = httpx.Client(base_url=args.tenant_api_base, timeout=30.0)

    apti_owner_password = "ApTi-Owner-" + uuid.uuid4().hex[:12]
    customer_owner_password = "ApTi-Customer-" + uuid.uuid4().hex[:12]

    print("1. Logging in as platform admin...")
    login = _post(admin, "/api/v1/admin/auth/login", {
        "email": args.platform_admin_email, "password": args.platform_admin_password,
    })
    admin_token = login["access_token"]

    apti_owner_token: str
    apti_organization_id: str

    if args.resume_apti_owner_token:
        if not args.resume_apti_organization_id:
            print("--resume-apti-owner-token requires --resume-apti-organization-id too.", file=sys.stderr)
            sys.exit(1)
        print("2. Resuming with an already-issued Apti owner token (skipping org creation).")
        apti_owner_token = args.resume_apti_owner_token
        apti_organization_id = args.resume_apti_organization_id
    else:
        print("2. Creating the Apti reseller organization...")
        org = _post(admin, "/api/v1/admin/organizations", {
            "organization_name": "Apti",
            "organization_type": "reseller",
            "owner_email": args.apti_owner_email,
            "owner_display_name": args.apti_owner_display_name,
        }, token=admin_token)
        print(json.dumps(org, indent=2))
        apti_organization_id = org["organization_id"]

        if org.get("invitation_link"):
            print("3. Dev fallback: no email provider configured, so the invitation link came back directly. "
                  "Accepting it now (this is the platform's own dev convenience, not a real inbox read).")
            owner_token = _invitation_token_from_link(org["invitation_link"], label="the Apti owner")
        else:
            print(
                f"An invitation email was sent to {args.apti_owner_email} - a real deployment stops here.\n"
                "Accept it (the owner sets their own password through the normal UI flow), then re-run this "
                "script with:\n"
                f"  --resume-apti-owner-token <the owner's access token> "
                f"--resume-apti-organization-id {apti_organization_id}\n"
                "to continue from branding onward."
            )
            sys.exit(0)

        apti_owner_token = _accept_invitation(
            tenant, token=owner_token, password=apti_owner_password, label="Apti's owner"
        )

    print("4. Setting Apti's branding...")
    branding = _put(admin, f"/api/v1/admin/organizations/{apti_organization_id}/branding", {
        "slug": args.branding_slug,
        "display_name": args.branding_display_name,
        "email_from_name": args.branding_email_from_name,
        "color_primary": args.branding_color_primary,
        "color_accent": args.branding_color_accent,
        "color_canvas": args.branding_color_canvas,
    }, token=admin_token)
    print(json.dumps(branding, indent=2))

    customer_owner_token: str

    if args.resume_customer_owner_token:
        print("5. Resuming with an already-issued test-customer owner token (skipping child-tenant creation).")
        customer_owner_token = args.resume_customer_owner_token
    else:
        print("5. Creating a real test tenant under Apti (a reseller_customer)...")
        child = _post(tenant, "/api/v1/tenant/child-tenants", {
            "organization_name": args.customer_org_name,
            "owner_email": args.customer_owner_email,
            "owner_display_name": args.customer_owner_display_name,
        }, token=apti_owner_token)
        print(json.dumps(child, indent=2))

        if child.get("invitation_link"):
            print("6. Dev fallback: following the test customer owner's invitation link the same way.")
            customer_token_raw = _invitation_token_from_link(
                child["invitation_link"], label="the test customer's owner"
            )
        else:
            print(
                f"An invitation email was sent to {args.customer_owner_email} - a real deployment stops here.\n"
                "Accept it, then re-run this script with:\n"
                f"  --resume-apti-owner-token {apti_owner_token} --resume-apti-organization-id {apti_organization_id} "
                f"--resume-customer-owner-token <the test customer owner's access token>\n"
                "(plus the same --camera-* arguments) to add the site/camera."
            )
            sys.exit(0)

        customer_owner_token = _accept_invitation(
            tenant, token=customer_token_raw, password=customer_owner_password,
            label="The test customer's owner",
        )

    print("7. Creating a site under the test customer tenant...")
    site = _post(tenant, "/api/v1/tenant/sites", {
        "name": args.camera_site_name,
        "code": args.camera_site_code,
        "timezone": args.camera_site_timezone,
    }, token=customer_owner_token)
    print(json.dumps(site, indent=2, default=str))

    print("8. Registering the camera (connection details stored, never reachability-checked here - "
          "see this script's own module docstring)...")
    camera = _post(tenant, "/api/v1/tenant/cameras", {
        "site_id": site["id"],
        "name": args.camera_name,
        "code": args.camera_code,
        "hostname": args.camera_hostname,
        "rtsp_port": args.camera_rtsp_port,
        "username": args.camera_username,
        "main_stream_path": args.camera_main_stream_path,
        "sub_stream_path": args.camera_sub_stream_path,
    }, token=customer_owner_token)
    print(json.dumps(camera, indent=2, default=str))

    print("9. Storing the camera's password (write-only - it can never be read back via the API)...")
    _put(tenant, f"/api/v1/tenant/cameras/{camera['id']}/credentials", {
        "username": args.camera_username,
        "password": args.camera_password,
    }, token=customer_owner_token)

    print("\nDone. Generated credentials (printed once - store them now):")
    print(f"  Apti owner          : {args.apti_owner_email if not args.resume_apti_owner_token else '(resumed)'} "
          f"/ {apti_owner_password if not args.resume_apti_owner_token else '(unknown - resumed run)'}")
    print(f"  Test customer owner : "
          f"{args.customer_owner_email if not args.resume_customer_owner_token else '(resumed)'} "
          f"/ {customer_owner_password if not args.resume_customer_owner_token else '(unknown - resumed run)'}")
    print(f"  Apti organization_id: {apti_organization_id}")
    print(f"  Site id             : {site['id']}")
    print(f"  Camera id           : {camera['id']}")
    print(f"\nVerify branding is live: curl {args.tenant_api_base}/api/v1/public/branding/{args.branding_slug}")


if __name__ == "__main__":
    main()
