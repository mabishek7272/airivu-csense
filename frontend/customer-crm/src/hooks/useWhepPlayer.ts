import { useEffect, useState, type RefObject } from "react";

export type PlayerStatus = "idle" | "connecting" | "live" | "error";

/** Plays a MediaMTX WHEP (WebRTC-HTTP Egress Protocol) session in a `<video>` element.
 *
 *  Hand-rolled rather than a library - WHEP is a small enough HTTP+SDP exchange
 *  (offer/answer, no signalling server, no ICE trickle needed for MediaMTX's own
 *  implementation) that a dependency would be pulling in more than this needs, matching
 *  this project's general dependency discipline elsewhere (e.g. no drag-and-drop DAG
 *  library for one pipeline stage type in Phase 4).
 */
export function useWhepPlayer(
  videoRef: RefObject<HTMLVideoElement>,
  playUrl: string | null,
): { status: PlayerStatus; errorMessage: string | null } {
  const [status, setStatus] = useState<PlayerStatus>("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    const video = videoRef.current;
    if (!playUrl || !video) {
      setStatus("idle");
      return;
    }

    setStatus("connecting");
    setErrorMessage(null);

    // Narrowed to a plain `const` so TypeScript (and the closure below) can trust it stays
    // non-null - `playUrl` itself is `string | null` and doesn't narrow across the async
    // closure boundary on its own.
    const url = playUrl;
    let cancelled = false;
    let pc: RTCPeerConnection | null = null;
    let resourceUrl: string | null = null;

    async function connect() {
      pc = new RTCPeerConnection();
      pc.addTransceiver("video", { direction: "recvonly" });

      pc.ontrack = (event) => {
        if (video && event.streams[0]) video.srcObject = event.streams[0];
      };
      pc.onconnectionstatechange = () => {
        if (cancelled || !pc) return;
        if (pc.connectionState === "connected") setStatus("live");
        if (pc.connectionState === "failed" || pc.connectionState === "closed") {
          setStatus("error");
          setErrorMessage("The live connection was lost.");
        }
      };

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      // MediaMTX's WHEP endpoint answers once ICE gathering settles enough of the offer
      // to be useful - waiting for the full `icegatheringstate === "complete"` keeps this
      // simple (one request, no trickle-ICE signalling channel needed) at the cost of a
      // little extra latency versus trickling candidates as they arrive.
      await new Promise<void>((resolve) => {
        if (!pc || pc.iceGatheringState === "complete") {
          resolve();
          return;
        }
        const check = () => {
          if (pc?.iceGatheringState === "complete") {
            pc.removeEventListener("icegatheringstatechange", check);
            resolve();
          }
        };
        pc.addEventListener("icegatheringstatechange", check);
      });

      if (cancelled || !pc?.localDescription) return;

      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/sdp" },
        body: pc.localDescription.sdp,
      });
      if (!response.ok) {
        throw new Error(`WHEP endpoint returned ${response.status}`);
      }
      const location = response.headers.get("Location");
      resourceUrl = location ? new URL(location, url).toString() : null;

      const answerSdp = await response.text();
      if (cancelled || !pc) return;
      await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
    }

    connect().catch(() => {
      if (!cancelled) {
        setStatus("error");
        setErrorMessage("Could not start the live connection.");
      }
    });

    return () => {
      cancelled = true;
      pc?.close();
      if (resourceUrl) void fetch(resourceUrl, { method: "DELETE" }).catch(() => {});
    };
  }, [playUrl, videoRef]);

  return { status, errorMessage };
}
