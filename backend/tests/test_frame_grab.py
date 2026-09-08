"""Real I/O tests for `csense_shared.cameras.frame_grab.grab_frame` - no mocked `cv2`,
per this project's own "verify for real, not by inspection" standard (CLAUDE.md).

**Why this doesn't literally reuse the dev stack's own `mediamtx` container.** The compose
service's RTSP port (8554) is deliberately *not* published to the host (see
`infra/docker-compose.yml`'s own comment on that decision, and `infra/mediamtx/
mediamtx.yml`'s matching note) - nothing in the running system needs to reach it from
outside the Docker network, and `scripts/e2e_live_view.py`'s own `ffprobe()` helper proves
it: it reaches that RTSP path by running `ffprobe` *inside* the mediamtx container
(`docker compose exec`), not from the host. A `pytest` process on the host has no such
route in. So this fixture runs a second, throwaway instance of the exact same pinned image
(`bluenviron/mediamtx:1.20.1-ffmpeg` - already pulled by `docker compose up`, no network
fetch needed) with its RTSP port bound to a free host port for the test's own lifetime
only, and publishes a real test pattern into it with the host's own `ffmpeg` - the same
"ffmpeg re-streams a real video into a real RTSP server" shape `e2e_live_view.py` uses
against the real NVR, just pointed at a throwaway local server instead of a shared one
that (correctly) isn't host-reachable.

**Why the publisher forces a short GOP (`-g 15 -keyint_min 15`, one keyframe/second at
15fps).** Found by testing, not assumed: FFmpeg's default `libx264` GOP is 250 frames
(~16.6s at 15fps), and a fresh RTSP connection cannot decode a single frame until the next
keyframe arrives. Against the default GOP, `grab_frame` failed roughly half the time in a
tight loop - not a bug in `grab_frame` (proven by cross-checking with `curl`'s own read
against the same stream: the timeout consistently landed exactly at `timeout_seconds`,
and the very next connection attempt - landing closer to the next keyframe - succeeded in
under 3s every time), but an artifact of this fixture's own test pattern having a
GOP longer than the read timeout anyone would reasonably set. A 1s GOP matches CLAUDE.md's
own "shortening it to 1s GOP" recommendation for the real reference NVR, and makes this
fixture's timing realistic instead of coin-flip flaky.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

import numpy as np
import psutil
import pytest

from csense_shared.cameras.frame_grab import grab_frame

MEDIAMTX_IMAGE = "bluenviron/mediamtx:1.20.1-ffmpeg"  # same pinned tag as infra/docker-compose.yml

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or shutil.which("ffmpeg") is None,
    reason="docker and a host ffmpeg are both required to publish a real RTSP test stream",
)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_tcp(host: str, port: int, *, timeout: float) -> None:
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
                import json

                if json.loads(resp.read()).get("ready"):
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"mediamtx path '{path_name}' never became ready")


@pytest.fixture(scope="module")
def real_rtsp_stream(tmp_path_factory: pytest.TempPathFactory):
    """A real, actually-published RTSP stream: a throwaway MediaMTX server plus a real
    ffmpeg publisher pushing a synthetic test pattern into it. Yields the `rtsp://` URL a
    consumer (like `grab_frame`) can dial directly.
    """
    rtsp_port = _free_tcp_port()
    api_port = _free_tcp_port()
    container_name = f"csense-frame-grab-test-{uuid.uuid4().hex[:8]}"

    config_dir = tmp_path_factory.mktemp("mediamtx-test-config")
    config_path = config_dir / "mediamtx.yml"
    # Deliberately permissive (no auth) - this instance is bound to 127.0.0.1 only, lives
    # for this test module's duration only, and carries no real camera credentials.
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

    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", container_name,
            "-p", f"127.0.0.1:{rtsp_port}:8554",
            "-p", f"127.0.0.1:{api_port}:9997",
            "-v", f"{config_path}:/mediamtx.yml:ro",
            MEDIAMTX_IMAGE,
        ],
        check=True, capture_output=True, text=True,
    )

    publisher: subprocess.Popen | None = None
    try:
        _wait_for_tcp("127.0.0.1", rtsp_port, timeout=15.0)

        path_name = "testpath"
        publisher = subprocess.Popen(
            [
                "ffmpeg", "-y", "-re", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
                "-t", "120", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-g", "15", "-keyint_min", "15",
                "-f", "rtsp", "-rtsp_transport", "tcp",
                f"rtsp://127.0.0.1:{rtsp_port}/{path_name}",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait_for_path_ready(api_port, path_name, timeout=15.0)

        yield f"rtsp://127.0.0.1:{rtsp_port}/{path_name}"
    finally:
        if publisher is not None:
            publisher.terminate()
            try:
                publisher.wait(timeout=5)
            except subprocess.TimeoutExpired:
                publisher.kill()
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, check=False)


def test_grab_frame_returns_a_real_decoded_frame(real_rtsp_stream):
    frame = grab_frame(real_rtsp_stream, timeout_seconds=10.0)

    assert frame is not None
    assert isinstance(frame, np.ndarray)
    assert frame.ndim == 3 and frame.shape[2] == 3  # BGR
    assert frame.shape[0] > 0 and frame.shape[1] > 0
    assert frame.dtype == np.uint8
    # testsrc is a colour-bar/gradient pattern - a genuinely decoded frame has real
    # variation, not the near-zero variance of an all-black or otherwise blank buffer.
    assert float(frame.std()) > 10.0


def test_grab_frame_on_a_dead_host_never_hangs_or_raises():
    """A closed port refuses the TCP connection immediately - real, but too fast to prove
    the *timeout* mechanism does anything. This is the case CLAUDE.md's own "an unreachable
    camera is a health fact, not a crash" framing is really about: a device that accepted
    the TCP connection but will never complete the RTSP handshake, e.g. a firewall that
    accepts and drops, or the intermediate hop of a broken tunnel."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]

    stop = threading.Event()
    accepted: list[socket.socket] = []

    def _accept_and_never_answer() -> None:
        # Accepted connections MUST be kept referenced: `server.accept()` on its own
        # (result discarded) leaves the returned socket with a zero refcount, and CPython
        # closes it - sending a real FIN/RST - essentially immediately. That isn't a black
        # hole, it's a fast, real disconnect (found by testing: the "black hole" looked
        # like it was timing out near-instantly, and it turned out the socket was never
        # actually held open). Appending to `accepted` is what makes this a genuine
        # accept-then-never-respond peer.
        server.settimeout(0.3)
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
                accepted.append(conn)
            except TimeoutError:
                continue
            except OSError:
                return

    acceptor = threading.Thread(target=_accept_and_never_answer, daemon=True)
    acceptor.start()
    try:
        timeout_seconds = 2.5
        started = time.monotonic()
        result = grab_frame(f"rtsp://127.0.0.1:{port}/blackhole", timeout_seconds=timeout_seconds)
        elapsed = time.monotonic() - started

        assert result is None
        # Bounded close to the configured timeout - not the ~0.01s a refused connection
        # would return in (which would prove nothing), and not tens of seconds (which
        # would mean the timeout wasn't actually being applied).
        assert timeout_seconds * 0.5 < elapsed < timeout_seconds + 5.0
    finally:
        stop.set()
        acceptor.join(timeout=2)
        server.close()


def test_grab_frame_on_a_refused_port_returns_none_fast():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    # The socket above is now closed and nothing is listening on `closed_port`.

    started = time.monotonic()
    result = grab_frame(f"rtsp://127.0.0.1:{closed_port}/nope", timeout_seconds=5.0)
    elapsed = time.monotonic() - started

    assert result is None
    assert elapsed < 5.0  # refused, not timed out


def test_grab_frame_releases_the_capture_on_every_path(real_rtsp_stream):
    """Proves the capture is genuinely released - on success, on a hard timeout, and on a
    fast refusal - by counting the test process's own open file descriptors before and
    after repeated calls of each kind. A leaked `VideoCapture` holds a real socket (and,
    on most platforms, more than one fd for its decoder machinery); a leak would show up
    as fd count climbing with every round, not as a one-time bump.

    The very first call is deliberately excluded from the comparison: OpenCV's FFmpeg
    backend allocates some fixed, one-time internal state (confirmed by testing - fd count
    rises once on first use and then stays flat for dozens of subsequent calls, success or
    failure), which is normal library warm-up, not a per-call leak.
    """
    if not hasattr(psutil.Process(), "num_fds"):
        pytest.skip("num_fds() is only available on POSIX platforms")

    process = psutil.Process()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    blackhole_port = server.getsockname()[1]
    stop = threading.Event()
    accepted: list[socket.socket] = []

    def _accept_and_never_answer() -> None:
        # See the identical comment on the other black-hole fixture above: the accepted
        # socket must be kept referenced or CPython closes it immediately, which fails
        # fast instead of hanging until the real timeout.
        server.settimeout(0.3)
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
                accepted.append(conn)
            except TimeoutError:
                continue
            except OSError:
                return

    acceptor = threading.Thread(target=_accept_and_never_answer, daemon=True)
    acceptor.start()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]

    def _one_round() -> None:
        grab_frame(real_rtsp_stream, timeout_seconds=10.0)  # success path
        grab_frame(f"rtsp://127.0.0.1:{blackhole_port}/blackhole", timeout_seconds=1.5)  # timeout path
        grab_frame(f"rtsp://127.0.0.1:{closed_port}/nope", timeout_seconds=1.5)  # refused path
        # Close out this round's server-side accepted socket(s) immediately - they exist
        # only to make the black hole real for the call just made, and this test measures
        # *grab_frame's own* fd usage. Left open, every round adds one server-side fd of
        # the test fixture's own making and would misreport as a leak in `grab_frame`
        # (found by testing this exact assertion: fd count climbed by exactly one per
        # round, matching one retained accepted socket per round, not per-call growth
        # inside `grab_frame` itself).
        while accepted:
            accepted.pop().close()

    try:
        _one_round()  # warm-up: absorbs the one-time library-init fd bump described above
        baseline = process.num_fds()

        for _ in range(5):
            _one_round()

        after = process.num_fds()
        assert after <= baseline, (
            f"open fd count grew from {baseline} to {after} across 5 rounds of "
            "success/timeout/refused grab_frame calls - the capture is not being released"
        )
    finally:
        stop.set()
        acceptor.join(timeout=2)
        server.close()


def test_grab_frame_produces_the_same_result_shape_for_repeated_calls(real_rtsp_stream):
    """Not a formal idempotency requirement (that belongs to the runtime loop built on top
    of this, see Task 2) - just confirms `grab_frame` is safely callable more than once
    against the same live source without state leaking between calls."""
    first = grab_frame(real_rtsp_stream, timeout_seconds=10.0)
    second = grab_frame(real_rtsp_stream, timeout_seconds=10.0)

    assert first is not None and second is not None
    assert first.shape == second.shape
