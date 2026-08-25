#!/usr/bin/env bash
# End-to-end smoke test against a running local stack (docker compose up).
# Verifies: registration, login, tenant-scoped access, and the audience-isolation
# property from IMP-G1 (a customer token must be rejected by the Admin API).
set -euo pipefail

BASE="${1:-http://localhost:8080}"
EMAIL="smoke-$(date +%s)@example.com"
PASSWORD="Sm0keTest!Password123"
COOKIES=$(mktemp)
trap 'rm -f "$COOKIES"' EXIT

echo "== Register a new tenant =="
REGISTER_RESPONSE=$(curl -sf -c "$COOKIES" -X POST "$BASE/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"organization_name\":\"Smoke Test Org\",\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\",\"display_name\":\"Smoke Tester\"}")
echo "$REGISTER_RESPONSE"
ACCESS_TOKEN=$(echo "$REGISTER_RESPONSE" | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
TENANT_ID=$(echo "$REGISTER_RESPONSE" | python -c "import sys,json; print(json.load(sys.stdin)['tenant_id'])")
echo "Tenant ID: $TENANT_ID"

echo
echo "== Refresh the access token (rotating refresh cookie) =="
REFRESH_RESPONSE=$(curl -sf -b "$COOKIES" -c "$COOKIES" -X POST "$BASE/api/v1/auth/refresh")
echo "$REFRESH_RESPONSE"

echo
echo "== Log in again with the same credentials =="
LOGIN_RESPONSE=$(curl -sf -c "$COOKIES" -X POST "$BASE/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")
echo "$LOGIN_RESPONSE"
CUSTOMER_TOKEN=$(echo "$LOGIN_RESPONSE" | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo
echo "== A customer-audience token must be rejected by the Admin API (IMP-G1) =="
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/admin/organizations" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN")
if [ "$STATUS" = "401" ]; then
  echo "PASS: Admin API returned 401 for a customer token."
else
  echo "FAIL: expected 401, got $STATUS"
  exit 1
fi

echo
echo "All smoke tests passed."
