/** Where the demo API lives. Override per build with EXPO_PUBLIC_DEMO_API_BASE. */
export const API_BASE = process.env.EXPO_PUBLIC_DEMO_API_BASE ?? 'https://demo.3rdi.in';

/** Frames per second the live loop aims for. The server rate-limits at 150/min per
 *  address (2.5/s), and each frame also costs shared CPU, so stay comfortably below. */
export const LIVE_INTERVAL_MS = 600;

/** Longest side of a frame sent for detection: small enough to stay under the server's
 *  1.5 MB cap and fast on mobile data, large enough for a phone or a face at arm's length. */
export const LIVE_FRAME_WIDTH = 640;
export const UPLOAD_MAX_WIDTH = 1280;
