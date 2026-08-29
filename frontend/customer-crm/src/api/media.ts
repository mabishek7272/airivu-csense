import { apiFetch } from "./client";

/** Live-view sessions, brokered through the Media Session Service (TRD §14).
 *
 *  `protocol` genuinely changes what this costs the server: `hls` relays the camera's own
 *  stream with no transcoding; `webrtc` drives a real ffmpeg transcode of the camera's
 *  substream, because the deployment's real NVR streams H.265, which no mainstream
 *  browser's WebRTC stack can decode (see backend/tenant_api/app/api/media.py).
 */

export type LiveViewProtocol = "hls" | "webrtc";

export interface LiveSession {
  path: string;
  protocol: LiveViewProtocol;
  token: string;
  /** Already carries `?token=...` - hand this straight to the HLS/WHEP player. */
  play_url: string;
  expires_in: number;
}

export function startLiveSession(cameraId: string, protocol: LiveViewProtocol) {
  return apiFetch<LiveSession>(`/api/v1/tenant/cameras/${cameraId}/live-session`, {
    method: "POST",
    body: JSON.stringify({ protocol }),
  });
}
