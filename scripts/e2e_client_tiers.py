"""Client tiers, verified against a running stack: `standard` allows 2 phones + 1 concurrent
browser, `industry` 5 + 2, and a tenant with no plan is unlimited.

Runs in phases so the licence can be attached between them with real SQL (licences are
normally issued through the admin API, which needs an MFA step-up this script does not
need to exercise):

    python scripts/e2e_client_tiers.py register  <base_url> <state.json>
    python scripts/e2e_client_tiers.py unlimited <base_url> <state.json>
    python scripts/e2e_client_tiers.py limits    <base_url> <state.json> <mobile_limit> <web_limit>

Exits non-zero on the first failed check.
"""
from __future__ import annotations

import json
import sys
import uuid

import httpx

PASSWORD = "TierE2E!Password-4471"
failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
    if not ok:
        failures.append(what)


def cookies_from(resp: httpx.Response) -> dict[str, str]:
    out = {}
    for header in resp.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        out[name.strip()] = rest.split(";")[0]
    return out


def login(c: httpx.Client, email: str, **extra) -> httpx.Response:
    return c.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD, **extra})


def refresh(c: httpx.Client, cookies: dict[str, str]) -> int:
    header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return c.post("/api/v1/auth/refresh", headers={"Cookie": header}).status_code


def main() -> int:
    phase, base, state_path = sys.argv[1], sys.argv[2], sys.argv[3]
    c = httpx.Client(base_url=base, timeout=30)

    if phase == "register":
        suffix = uuid.uuid4().hex[:8]
        email = f"tiers-{suffix}@example.com"
        r = c.post("/api/v1/auth/register", json={
            "organization_name": f"Tiers E2E {suffix}", "email": email,
            "password": PASSWORD, "display_name": "Tier Owner",
        })
        r.raise_for_status()
        json.dump({"email": email, "org": f"Tiers E2E {suffix}", "tenant_id": r.json()["tenant_id"]}, open(state_path, "w"))
        print(f"registered {email} tenant {r.json()['tenant_id']}")
        return 0

    state = json.load(open(state_path))
    email = state["email"]

    if phase == "unlimited":
        print("no plan assigned: unlimited")
        ids = [f"unlimited-device-{i}-{uuid.uuid4().hex[:6]}" for i in range(4)]
        codes = [login(c, email, client="mobile", device_id=d).status_code for d in ids]
        check(codes == [200] * 4, f"4 different phones all sign in with no plan (got {codes})")
        sessions = [cookies_from(login(c, email)) for _ in range(3)]
        codes = [refresh(c, s) for s in sessions]
        check(codes == [200] * 3, f"3 browsers all stay signed in with no plan (got {codes})")
        return 1 if failures else 0

    mobile_limit, web_limit = int(sys.argv[4]), int(sys.argv[5])
    print(f"plan limits: {mobile_limit} phones, {web_limit} browsers")

    print("mobile devices")
    device_ids = [f"tier-device-{i}-{uuid.uuid4().hex[:6]}" for i in range(mobile_limit + 1)]
    first = []
    for d in device_ids[:mobile_limit]:
        r = login(c, email, client="mobile", device_id=d, device_name=f"Phone {d[-6:]}", platform="ios")
        first.append(r)
    check(all(r.status_code == 200 for r in first), f"first {mobile_limit} phones sign in")
    over = login(c, email, client="mobile", device_id=device_ids[-1])
    check(over.status_code == 403 and over.json().get("code") == "device_limit_reached",
          f"phone #{mobile_limit + 1} is refused with device_limit_reached (got {over.status_code} {over.json().get('code')})")
    again = login(c, email, client="mobile", device_id=device_ids[0])
    check(again.status_code == 200, "an already-registered phone can sign in again without using a new slot")
    check(login(c, email, client="mobile").status_code == 422, "a mobile login without a device id is refused")

    token = first[0].json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    listing = c.get("/api/v1/tenant/devices", headers=h).json()
    check(len(listing["devices"]) == mobile_limit and listing["device_limit"] == mobile_limit
          and listing["web_session_limit"] == web_limit,
          f"device list shows {mobile_limit} devices and both limits (got {len(listing['devices'])}, {listing['device_limit']}, {listing['web_session_limit']})")

    victim_name = f"Phone {device_ids[mobile_limit - 1][-6:]}"
    victim = next(d for d in listing["devices"] if d["name"] == victim_name)
    victim_cookies = cookies_from(login(c, email, client="mobile", device_id=device_ids[mobile_limit - 1]))
    check(refresh(c, victim_cookies) == 200, "the phone about to be removed has a working session")
    token = login(c, email, client="mobile", device_id=device_ids[0]).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    removed = c.delete(f"/api/v1/tenant/devices/{victim['id']}", headers=h)
    check(removed.status_code == 204, f"removing a device succeeds (got {removed.status_code})")
    check(refresh(c, victim_cookies) == 401, "the removed phone's session is signed out")
    now_in = login(c, email, client="mobile", device_id=device_ids[-1])
    check(now_in.status_code == 200, "the freed slot lets the previously-refused phone in")

    print("web sessions")
    sessions = [cookies_from(login(c, email)) for _ in range(web_limit + 1)]
    codes = [refresh(c, s) for s in sessions]
    expected = [401] + [200] * web_limit
    check(codes == expected, f"oldest browser is signed out, newest {web_limit} stay in (got {codes}, want {expected})")
    check(now_in.status_code == 200 and refresh(c, cookies_from(now_in)) == 200,
          "a phone session is untouched by browser sign-ins")

    print("PASS" if not failures else f"FAIL ({len(failures)})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
