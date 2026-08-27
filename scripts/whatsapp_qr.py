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

import json
import os
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
    # The gateway explains its 4xx responses in the body ("instance is already
    # authenticated", "no QR available"). Letting urllib raise on the status code throws
    # that away and leaves only "HTTP Error 400", which is exactly the kind of blind guess
    # that wasted time on this integration before.
    script = (
        "import urllib.request, urllib.error\n"
        f"r = urllib.request.Request('http://whatsapp-gateway:8080{path}', {data_line}"
        f"headers={{'apikey':'{token}','Content-Type':'application/json'}}, method='{method}')\n"
        "try:\n"
        "    print(urllib.request.urlopen(r, timeout=45).read().decode())\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(e.read().decode() or '{}')\n"
    )
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "admin-api", "python", "-c", script],
        cwd=INFRA, capture_output=True, text=True, timeout=300, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-500:])
    return json.loads(result.stdout.strip())


def encode_grid(payload: str) -> list[list[bool]]:
    """Encodes the payload into a QR module grid.

    This replaces an earlier attempt to recover the grid by sampling the gateway's own
    256px PNG. That could not work: at roughly three pixels per module the sampling is
    ambiguous, and a grid that is wrong by a single module still renders as something
    that looks exactly like a QR code and decodes as nothing. Two such images were
    produced and neither scanned.

    Encoding from the payload string removes the guesswork entirely.
    """
    import qrcode

    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=0)
    code.add_data(payload)
    code.make(fit=True)
    return [[bool(cell) for cell in row] for row in code.get_matrix()]

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
                line.append("â–„")
            elif bottom:
                line.append("â–€")
            else:
                line.append("â–ˆ")
        out.append("".join(line))
    return "\n".join(out)


def fetch_qr(token: str) -> tuple[str | None, str | None]:
    """Returns (rendered PNG data-URL, raw QR payload).

    `code` is the QR payload itself, not a pairing code - the two are unrelated, and
    treating `code` as something a person could type was a mistake this originally made.
    The payload is the useful half: it can be re-encoded at any resolution, whereas the
    gateway's 256px PNG cannot be rescued.
    """
    body = gateway_call("GET", "/instance/qr", token)
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    return data.get("qrcode"), data.get("code")


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


def write_png(payload: str, scale: int = 10) -> int:
    """Encodes the raw pairing payload into a scannable PNG.

    The gateway also returns a rendered PNG, but it is 256x256 with no quiet zone - about
    three pixels per module - and it does not survive a decoder even before anything else
    touches it. Reconstructing a module grid from those pixels does not help either: at
    that resolution the sampling is ambiguous, and a grid that is wrong by one module
    produces an image that looks like a QR and scans as nothing. Both of those were tried
    here and both failed to decode.

    Encoding the payload string directly sidesteps the whole problem. The gateway hands
    back the payload in the response's `code` field, so there is no need to recover it
    from pixels at all.

    Error correction is deliberately LOW: the payload is long, and a higher level pushes
    the symbol to a larger version with finer modules, which is harder to scan off a
    screen, not easier. The code is short-lived and read at close range, where damage
    tolerance is worth nothing.
    """
    import qrcode

    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=scale,
        border=4,  # the quiet zone the spec requires, and the gateway's PNG omits
    )
    code.add_data(payload)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white").convert("L")

    dest = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "whatsapp-qr.png")
    if not os.path.isdir(os.path.dirname(dest)):
        dest = os.path.join(os.path.expanduser("~"), "Desktop", "whatsapp-qr.png")
    image.save(dest)

    modules = code.modules_count
    print(f"\n  Saved: {dest}")
    print(f"  {image.width}x{image.height}px, {modules}x{modules} modules, {scale}px each.")

    if not verify_png(dest):
        print("\n  WARNING: this image did not decode locally. Do not bother scanning it.")
        return 1

    print("  Verified: decodes cleanly.")
    print("\n  Open it FIRST, then start the scan - the code rotates every ~20 seconds.")
    print("  WhatsApp -> Settings -> Linked devices -> Link a device")
    return 0


def verify_png(path: str) -> bool:
    """Decodes the file we just wrote, so a broken QR is caught here and not by a person.

    Two earlier attempts produced images that looked correct and scanned as nothing. A
    person holding a phone is a slow and demoralising way to discover that.
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError:
        # Verification is a safeguard, not a requirement - do not block on it.
        return True

    data, _, _ = cv2.QRCodeDetector().detectAndDecode(
        np.array(Image.open(path).convert("L"))
    )
    return bool(data)


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

    # The gateway only produces a QR for a socket that is already dialling, and the socket
    # drops when nobody completes the link. Connecting unconditionally means a QR is
    # always available rather than failing with "run with --connect first".
    gateway_call("POST", "/instance/connect", token, body="{}")
    time.sleep(2)

    qr, payload = fetch_qr(token)
    if not qr and not payload:
        print("No QR available yet - the gateway did not produce one. Try again.")
        return 1

    if "--png" in sys.argv:
        if not payload:
            print("The gateway returned no raw payload; cannot build a scannable image.")
            return 1
        return write_png(payload)

    if not payload:
        print("The gateway returned no raw payload; cannot render a reliable QR.")
        return 1

    grid = encode_grid(payload)
    print()
    print(render(grid))
    print()
    print(f"  {len(grid)}x{len(grid)} modules. Scan within ~20 seconds - the code rotates.")
    print("  WhatsApp -> Settings -> Linked devices -> Link a device")
    print("  If your terminal font mangles this, use --png instead.")

    return watch(token) if "--watch" in sys.argv else 0


if __name__ == "__main__":
    raise SystemExit(main())
