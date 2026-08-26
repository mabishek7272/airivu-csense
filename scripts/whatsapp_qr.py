"""Fetches the WhatsApp pairing QR and renders it scannable in the terminal.

The gateway returns a PNG, but a terminal render is what actually gets scanned - it is
immediate, and it avoids saving a file that grants device-linking access to whoever finds
it later.

The QR rotates roughly every 20 seconds, so this re-fetches on each run rather than
caching. Nothing is persisted.

    python scripts/whatsapp_qr.py [--connect] [--watch]
    python scripts/whatsapp_qr.py --pair +919876543210 [--watch]

`--pair` is the better option in practice. The QR rotates roughly every 20 seconds, which
is not enough time to render it, hand it to a person and have them open WhatsApp. A
pairing code is eight characters, typed in, and lasts minutes.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
INFRA = os.path.join(REPO, "infra")


def env(key: str) -> str:
    with open(os.path.join(REPO, ".env"), encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise KeyError(key)


def gateway_call(method: str, path: str, token: str, body: str | None = None) -> dict:
    """Calls the gateway from inside the Docker network - it has no public route."""
    data_line = f"data={body.encode()!r}," if body else ""
    script = (
        "import urllib.request\n"
        f"r = urllib.request.Request('http://whatsapp-gateway:8080{path}', {data_line}"
        f"headers={{'apikey':'{token}','Content-Type':'application/json'}}, method='{method}')\n"
        "print(urllib.request.urlopen(r, timeout=45).read().decode())\n"
    )
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "admin-api", "python", "-c", script],
        cwd=INFRA, capture_output=True, text=True, timeout=300, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-500:])
    return json.loads(result.stdout.strip())


def decode_modules(png_bytes: bytes) -> list[list[bool]]:
    """Recovers the QR module grid from a scaled-up PNG.

    The scale factor is derived from the top-left finder pattern, which is always exactly
    7 modules wide. Guessing the scale from image width alone gives non-QR sizes - QR
    versions are 4n+17 modules, so 128 is not a possible answer.
    """
    from PIL import Image

    image = Image.open(io.BytesIO(png_bytes)).convert("L")
    width, height = image.size
    pixels = image.load()

    def dark(x: int, y: int) -> bool:
        return pixels[x, y] < 128

    # Trim the quiet zone: find the bounding box of dark pixels.
    left = next(x for x in range(width) if any(dark(x, y) for y in range(height)))
    top = next(y for y in range(height) if any(dark(x, y) for x in range(width)))
    right = next(x for x in range(width - 1, -1, -1) if any(dark(x, y) for y in range(height)))
    bottom = next(y for y in range(height - 1, -1, -1) if any(dark(x, y) for x in range(width)))

    span = right - left + 1

    # The first dark run across the finder pattern is 7 modules wide.
    run = 0
    while left + run <= right and dark(left + run, top + 1):
        run += 1
    module_px = max(1, round(run / 7))
    modules = round(span / module_px)

    # QR versions are 4n+17 modules. Snap to the nearest valid size rather than trusting
    # the arithmetic exactly - anti-aliasing can shift the run length by a pixel.
    valid = [4 * n + 17 for n in range(1, 41)]
    modules = min(valid, key=lambda v: abs(v - modules))
    step = span / modules

    grid = []
    for row in range(modules):
        line = []
        for col in range(modules):
            x = min(right, int(left + (col + 0.5) * step))
            y = min(bottom, int(top + (row + 0.5) * step))
            line.append(dark(x, y))
        grid.append(line)
    return grid


def render(grid: list[list[bool]]) -> str:
    """Half-block rendering: two module rows per text line keeps the aspect square.

    Printed light-on-dark (inverted) because most terminals have a dark background, and a
    phone camera needs the QR's own quiet zone to be the lighter of the two.
    """
    size = len(grid)
    quiet = 2
    out = []
    padded = (
        [[False] * (size + quiet * 2) for _ in range(quiet)]
        + [[False] * quiet + row + [False] * quiet for row in grid]
        + [[False] * (size + quiet * 2) for _ in range(quiet)]
    )
    for y in range(0, len(padded), 2):
        line = []
        for x in range(len(padded[0])):
            top = padded[y][x]
            bottom = padded[y + 1][x] if y + 1 < len(padded) else False
            # Inverted: a dark module prints as blank space on a light block background.
            if top and bottom:
                line.append(" ")
            elif top:
                line.append("▄")
            elif bottom:
                line.append("▀")
            else:
                line.append("█")
        out.append("".join(line))
    return "\n".join(out)


def fetch_qr(token: str) -> tuple[str | None, str | None]:
    body = gateway_call("GET", "/instance/qr", token)
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    return data.get("qrcode"), data.get("pairingCode") or data.get("code")


def pair(token: str, phone: str) -> str | None:
    """Requests an 8-character pairing code for a phone number."""
    digits = phone.lstrip("+")
    body = json.dumps({"phone": digits})
    response = gateway_call("POST", "/instance/pair", token, body=body)
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    return data.get("PairingCode") or data.get("pairingCode") or data.get("code")


def watch(token: str, attempts: int = 60) -> int:
    print(chr(10) + "  Watching for connection...")
    for _ in range(attempts):
        time.sleep(3)
        status = gateway_call("GET", "/instance/status", token)
        state = status.get("data", status)
        # LoggedIn, not Connected: Connected is only the websocket, which is already up
        # while the pairing code sits unentered.
        if state.get("LoggedIn") or state.get("loggedIn"):
            print(f"  LINKED as {state.get('Name') or state.get('name') or 'unknown'}")
            return 0
    print("  Not connected yet - re-run for a fresh code.")
    return 1


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    token = env("WHATSAPP_INSTANCE_TOKEN")

    if "--connect" in sys.argv:
        gateway_call("POST", "/instance/connect", token, body="{}")

    status = gateway_call("GET", "/instance/status", token)
    state = status.get("data", status)
    if state.get("LoggedIn") or state.get("loggedIn"):
        print("Already linked. Nothing to scan.")
        return 0

    if "--pair" in sys.argv:
        phone = sys.argv[sys.argv.index("--pair") + 1]
        # The gateway only issues a pairing code for a socket that is already dialling.
        gateway_call("POST", "/instance/connect", token, body="{}")
        time.sleep(2)
        code = pair(token, phone)
        if not code:
            print("The gateway returned no pairing code. Try the QR path instead.")
            return 1
        print()
        print(f"    PAIRING CODE:  {code}")
        print()
        print("  On the phone holding that number:")
        print("    WhatsApp -> Settings -> Linked devices -> Link a device")
        print("    -> Link with phone number instead -> enter the code above")
        return watch(token) if "--watch" in sys.argv else 0

    qr, pairing_code = fetch_qr(token)
    if not qr:
        print("No QR available yet. Run with --connect first.")
        return 1

    match = re.match(r"data:image/\w+;base64,(.+)", qr, re.DOTALL)
    if not match:
        print("Unexpected QR payload:", qr[:80])
        return 1

    grid = decode_modules(base64.b64decode(match.group(1)))
    print()
    print(render(grid))
    print()
    print(f"  {len(grid)}x{len(grid)} modules. Scan within ~20 seconds - the code rotates.")
    print("  WhatsApp -> Settings -> Linked devices -> Link a device")
    if pairing_code:
        print(f"  Or enter pairing code: {pairing_code}")

    if "--watch" in sys.argv:
        print("\n  Watching for connection...")
        for _ in range(60):
            time.sleep(3)
            status = gateway_call("GET", "/instance/status", token)
            state = status.get("data", status)
            if state.get("connected"):
                print(f"  CONNECTED as {state.get('jid') or 'unknown'}")
                return 0
        print("  Not connected yet - re-run for a fresh code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
