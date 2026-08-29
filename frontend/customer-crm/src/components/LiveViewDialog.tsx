import { useRef, useState } from "react";
import type { Camera } from "../api/cameras";
import { startLiveSession, type LiveSession, type LiveViewProtocol } from "../api/media";
import { Dialog } from "./Dialog";
import { InlineSpinner } from "./States";
import { useHlsPlayer } from "../hooks/useHlsPlayer";
import { useWhepPlayer } from "../hooks/useWhepPlayer";
import { ApiRequestError } from "../api/client";

/** Watching a camera live. Two distinct modes, not a fallback chain the user cannot see -
 *  they cost the server genuinely different amounts (see api/media.ts), so which one is
 *  running is always visible, never silently decided for the viewer. */
export function LiveViewDialog({ camera, onClose }: { camera: Camera; onClose: () => void }) {
  const [protocol, setProtocol] = useState<LiveViewProtocol | null>(null);
  const [session, setSession] = useState<LiveSession | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<unknown>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  async function pick(chosen: LiveViewProtocol) {
    setProtocol(chosen);
    setStarting(true);
    setStartError(null);
    try {
      const started = await startLiveSession(camera.id, chosen);
      setSession(started);
    } catch (err) {
      setStartError(err);
    } finally {
      setStarting(false);
    }
  }

  const hls = useHlsPlayer(videoRef, session?.protocol === "hls" ? session.play_url : null);
  const whep = useWhepPlayer(videoRef, session?.protocol === "webrtc" ? session.play_url : null);
  const player = session?.protocol === "webrtc" ? whep : hls;

  return (
    <Dialog open title={`Watch live: ${camera.name}`} onClose={onClose}>
      {!protocol && (
        <div className="live-view-picker">
          <p>How should this camera stream?</p>
          <div style={{ display: "flex", gap: 12 }}>
            <button type="button" onClick={() => void pick("hls")}>
              Standard (HLS)
              <span className="muted" style={{ display: "block", fontSize: 12 }}>
                No extra server load. May not play in every browser depending on this
                camera&apos;s video format.
              </span>
            </button>
            <button type="button" onClick={() => void pick("webrtc")}>
              Low-latency (WebRTC)
              <span className="muted" style={{ display: "block", fontSize: 12 }}>
                Plays in any modern browser. Costs the server a live transcode while
                you&apos;re watching.
              </span>
            </button>
          </div>
        </div>
      )}

      {protocol && starting && <InlineSpinner label="Starting the live session…" />}

      {protocol && !starting && startError !== null && (
        <StartFailure error={startError} onRetry={() => void pick(protocol)} />
      )}

      {session && (
        <div className="live-view-player">
          {player.status === "connecting" && <InlineSpinner label="Connecting…" />}
          {player.status === "error" && (
            <p role="alert" className="error-panel">
              {player.errorMessage ?? "The stream could not be played."}
            </p>
          )}
          {/* Always mounted once a session exists, not just while "live" - both player
              hooks attach to this element directly and need it present from the start. */}
          <video
            ref={videoRef}
            autoPlay
            muted
            playsInline
            controls
            style={{
              width: "100%",
              maxHeight: "60vh",
              background: "#000",
              display: player.status === "error" ? "none" : "block",
            }}
          />
          <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            {session.protocol === "webrtc" ? "Low-latency (WebRTC)" : "Standard (HLS)"}
          </p>
        </div>
      )}
    </Dialog>
  );
}

function StartFailure({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const message =
    error instanceof ApiRequestError
      ? error.body.message
      : error instanceof Error
        ? error.message
        : "Could not start the live session.";
  return (
    <div className="error-panel" role="alert">
      <strong>Could not start live view.</strong>
      <p style={{ margin: "8px 0 0" }}>{message}</p>
      <button type="button" onClick={onRetry} style={{ marginTop: 12 }}>
        Try again
      </button>
    </div>
  );
}
