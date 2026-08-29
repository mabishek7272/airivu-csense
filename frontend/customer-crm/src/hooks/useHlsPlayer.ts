import Hls from "hls.js";
import { useEffect, useRef, useState, type RefObject } from "react";

export type PlayerStatus = "idle" | "connecting" | "live" | "error";

/** Plays a MediaMTX HLS session in a `<video>` element.
 *
 *  **Honest limitation, not glossed over**: the real deployment's cameras stream H.265
 *  ([[nvr-h265-constraint]]). HLS *carries* H.265 without any server-side transcoding, but
 *  whether a given browser can actually *decode* it is a separate, inconsistent story -
 *  Safari reliably can (Apple's own codec ecosystem); Chrome/Firefox often cannot, or can
 *  only with specific hardware decode support. Checked with `MediaSource.isTypeSupported`
 *  before attempting playback, so an unsupported browser gets an honest message pointing
 *  at the low-latency (WebRTC, always H.264) option instead of an opaque stall.
 */
export function useHlsPlayer(
  videoRef: RefObject<HTMLVideoElement>,
  playUrl: string | null,
): { status: PlayerStatus; errorMessage: string | null } {
  const [status, setStatus] = useState<PlayerStatus>("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const hlsRef = useRef<Hls | null>(null);

  useEffect(() => {
    const video = videoRef.current;
    if (!playUrl || !video) {
      setStatus("idle");
      return;
    }

    setStatus("connecting");
    setErrorMessage(null);

    const nativeHls = video.canPlayType("application/vnd.apple.mpegurl") !== "";

    if (nativeHls) {
      // Safari (and some mobile browsers) play HLS - including HEVC - through the plain
      // <video> element with no library involved.
      video.src = playUrl;
      video.onloadedmetadata = () => setStatus("live");
      video.onerror = () => {
        setStatus("error");
        setErrorMessage("The stream could not be played.");
      };
      return () => {
        video.removeAttribute("src");
        video.onloadedmetadata = null;
        video.onerror = null;
      };
    }

    if (!Hls.isSupported()) {
      setStatus("error");
      setErrorMessage("This browser cannot play HLS. Try low-latency mode instead.");
      return;
    }

    if (
      typeof MediaSource !== "undefined" &&
      !MediaSource.isTypeSupported('video/mp4; codecs="hvc1.1.6.L93.90"')
    ) {
      setStatus("error");
      setErrorMessage(
        "This browser cannot decode this camera's video format (H.265) over HLS. " +
          "Try low-latency mode instead, which transcodes to a format every browser supports.",
      );
      return;
    }

    const hls = new Hls();
    hlsRef.current = hls;
    hls.loadSource(playUrl);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      void video.play();
      setStatus("live");
    });
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (data.fatal) {
        setStatus("error");
        setErrorMessage("The stream could not be played.");
      }
    });

    return () => {
      hls.destroy();
      hlsRef.current = null;
    };
  }, [playUrl, videoRef]);

  return { status, errorMessage };
}
