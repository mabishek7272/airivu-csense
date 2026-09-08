"""Grabbing exactly one real, decoded frame from an RTSP stream.

Built for the pipeline execution runtime's per-camera polling loop
(`csense_shared.pipeline.runtime`, next in this plan): one frame, once per cycle, per
camera - not a continuous read loop. The caller is expected to have already turned a
camera's hostname into a dial-safe address via `resolve_camera_endpoint` (this package's
`connection` module) and built `rtsp_url` from *that* resolved IP, never the original
hostname - RTSP servers are device-bound, not virtually hosted the way HTTP is, so an IP
literal in the URL is the correct shape here, not a workaround. This module trusts the URL
it is given; it does no resolution or SSRF checking of its own.

**TCP transport is forced, not left to whatever OpenCV's FFmpeg backend defaults to.**
CLAUDE.md's own measured finding, from testing this exact reference NVR: "UDP over the
public internet smears frames." OpenCV's FFmpeg backend has no numeric `CAP_PROP` for
this - `rtsp_transport` is an FFmpeg `AVOption` string, not one of the small set of
properties the `VideoCapture(url, apiPreference, params)` numeric-params constructor
accepts - so the documented mechanism is the `OPENCV_FFMPEG_CAPTURE_OPTIONS` environment
variable, which OpenCV's FFmpeg backend reads at capture-open time and forwards into
`avformat_open_input`. `backend/tests/test_frame_grab.py` proves this by asking a real
RTSP server (MediaMTX) what transport it actually negotiated with `grab_frame`'s own
connection - via MediaMTX's `/v3/rtspsessions/list` API, which reports a live `transport`
field (`"TCP"`/`"UDP"`) per session from the server's own side of the handshake - not by
inspecting `grab_frame`'s source or the environment variable it sets.

**The capture is released unconditionally.** A `VideoCapture` that is never `.release()`-d
keeps a real socket and a decoder thread alive for the life of the process. With one
asyncio task polling per camera, a leak here is not a one-off - it is slow, silent fd and
thread exhaustion across every polling cycle. `try/finally` is not a style preference here.

**Never raises.** `camera_probe.py`'s own module docstring already establishes the
convention this matches: "the camera is unreachable" is information the operator (here,
the execution loop) needs, not an exception - an offline camera is the routine case this
function exists to handle cleanly, not a crash the loop's own per-camera isolation has to
absorb.
"""
from __future__ import annotations

import logging
import os

import cv2
import numpy as np

logger = logging.getLogger(__name__)


def grab_frame(rtsp_url: str, *, timeout_seconds: float = 10.0) -> np.ndarray | None:
    """Opens the stream, reads exactly one frame, and always releases the capture.

    Returns `None` - never raises - if the stream can't be opened, the open/read times
    out, or the frame that comes back is empty/undecodable. `timeout_seconds` bounds both
    the open (TCP connect + RTSP handshake) and the read, via OpenCV's own
    `CAP_PROP_OPEN_TIMEOUT_MSEC`/`CAP_PROP_READ_TIMEOUT_MSEC` - the documented, real
    per-capture timeout mechanism for the FFmpeg backend, not a `signal.alarm`/thread-based
    approximation of one.
    """
    timeout_ms = int(timeout_seconds * 1000)

    # An AVOption, not a numeric CAP_PROP - this is OpenCV's own documented route for
    # passing FFmpeg demuxer options through to avformat_open_input. Set on every call
    # (not once at import time) so this function's TCP-only behaviour never depends on
    # some other, earlier caller having set it first.
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

    capture: cv2.VideoCapture | None = None
    try:
        capture = cv2.VideoCapture(
            rtsp_url,
            cv2.CAP_FFMPEG,
            [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms,
            ],
        )
        if not capture.isOpened():
            return None

        ok, frame = capture.read()
        if not ok or frame is None or frame.size == 0:
            return None
        return frame
    except cv2.error as exc:
        # OpenCV raises its own exception type for a handful of decode-time failures
        # rather than just returning a falsy read - a malformed stream is still "the
        # camera is unreachable/unusable right now", not a bug in this function.
        logger.info("frame_grab_cv2_error", extra={"error": str(exc)[:200]})
        return None
    finally:
        if capture is not None:
            capture.release()
