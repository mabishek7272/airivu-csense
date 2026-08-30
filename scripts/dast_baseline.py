"""Real DAST baseline scan (CHECKLIST: "DAST baseline scan against local stack
(automatable now)") - OWASP ZAP's own `zap-api-scan.py`, driven from each service's real,
already-live OpenAPI document (`tenant-api`/`admin-api` both publish one), against the
real running containers on the same docker network - not a mocked target.

**Baseline, not a full active fuzz by design.** `-I` tells ZAP not to fail the run over
WARN/FAIL-level alerts - the same non-blocking, report-don't-gate precedent this
session's own CI security scanning already established (bandit/npm audit). This finds
real, common issues (missing security headers, verbose errors, permissive CORS, cookie
flags) from the API's own declared surface without the aggressive, side-effect-risking
half of testing an already-live stack that also holds real e2e-script-created data - that
half is what the separate, human-run independent penetration test (CHECKLIST, [NEEDS
EXTERNAL INPUT]) is for.

Reports land in `reports/dast/` (gitignored - point-in-time artifacts, not something to
track in git).

Needs the real stack running (`docker compose up`) and `docker` reachable.

    python scripts/dast_baseline.py
"""
from __future__ import annotations

import json
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(REPO_ROOT, "reports", "dast")

# Real, live OpenAPI documents this deployment already serves - not a spec written
# separately for this scan to drift from what the API actually does.
TARGETS = [
    ("tenant-api", "http://tenant-api:8000/api/v1/tenant/openapi.json"),
    ("admin-api", "http://admin-api:8000/api/v1/admin/openapi.json"),
]

DOCKER_NETWORK = "csense_csense"


def run_scan(name: str, openapi_url: str) -> dict:
    json_report = f"{name}-report.json"
    html_report = f"{name}-report.html"
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", DOCKER_NETWORK, "--user", "root",
            "-v", f"{REPORT_DIR}:/zap/wrk:rw",
            "zaproxy/zap-stable",
            "zap-api-scan.py", "-t", openapi_url, "-f", "openapi",
            "-J", json_report, "-r", html_report, "-I", "-s",
        ],
        capture_output=True, text=True, check=False, timeout=240,
    )
    tail = "\n".join(result.stdout.splitlines()[-40:])
    print(tail)
    # ZAP's own exit codes: 0 = no alerts, 1 = WARNs, 2 = FAILs - all real, all reported
    # below, none of them a reason to fail this script (that is what -I already tells ZAP
    # itself). Anything else means ZAP could not run the scan at all.
    if result.returncode not in (0, 1, 2):
        raise RuntimeError(f"zap-api-scan.py against {name} exited {result.returncode}:\n{result.stderr[-2000:]}")

    report_path = os.path.join(REPORT_DIR, json_report)
    if not os.path.exists(report_path):
        raise RuntimeError(f"No report written for {name} - the scan may not have reached the target.")
    with open(report_path, encoding="utf-8") as f:
        return json.load(f)


def summarize(name: str, report: dict) -> list[dict]:
    alerts = [alert for site in report.get("site", []) for alert in site.get("alerts", [])]
    by_risk: dict[str, int] = {}
    for alert in alerts:
        risk = alert.get("riskdesc", "Unknown").split(" ")[0]
        by_risk[risk] = by_risk.get(risk, 0) + 1

    print(f"\n{name}: {len(alerts)} distinct alert type(s)")
    for risk in ("High", "Medium", "Low", "Informational"):
        if risk in by_risk:
            print(f"    {risk}: {by_risk[risk]}")
    for alert in alerts:
        if alert.get("riskdesc", "").startswith(("High", "Medium")):
            print(f"    [{alert.get('riskdesc')}] {alert.get('name')} - {alert.get('count', '?')} instance(s)")
    return alerts


def main() -> int:
    os.makedirs(REPORT_DIR, exist_ok=True)
    print(f"Reports will be written to {REPORT_DIR}\n")

    all_alerts: dict[str, list[dict]] = {}
    for name, url in TARGETS:
        print(f"=== Scanning {name} ({url}) ===")
        report = run_scan(name, url)
        all_alerts[name] = summarize(name, report)

    total = sum(len(a) for a in all_alerts.values())
    print(f"\n{total} total distinct alert type(s) across {len(TARGETS)} service(s). "
          f"Full HTML/JSON reports are in reports/dast/ for review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
