import { File, Paths } from 'expo-file-system';
import { API_BASE } from './config';
import type { NormBox } from './geometry';

export interface Mode {
  code: string;
  label: string;
  description: string;
  available: boolean;
  note: string | null;
}

export interface Detection {
  label: string;
  confidence: number;
  box: NormBox;
}

export interface DetectResult {
  mode: string;
  width: number;
  height: number;
  inference_ms: number;
  detections: Detection[];
}

/** A refused-but-expected outcome (busy, rate limited): the caller skips this frame and
 *  tries again after `retryAfterMs`, rather than showing an error. */
export class Throttled extends Error {
  constructor(public retryAfterMs: number) {
    super('throttled');
  }
}

export class DemoError extends Error {}

export async function listModes(): Promise<Mode[]> {
  const res = await fetch(`${API_BASE}/api/v1/demo/modes`);
  if (!res.ok) throw new DemoError('Could not reach the demo service.');
  return res.json();
}

export async function detect(mode: string, imageUri: string, signal?: AbortSignal): Promise<DetectResult> {
  const form = new FormData();
  form.append('mode', mode);
  // Expo SDK 57's fetch polyfill only accepts a real Blob-like value for a file part
  // (something with a .bytes() method) - the older { uri, name, type } descriptor throws
  // "Unsupported FormDataPart implementation" now. expo-file-system's File implements
  // Blob and reads the local file's real bytes, so it satisfies that check.
  form.append('image', new File(imageUri) as unknown as Blob, 'frame.jpg');

  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/v1/demo/detect`, { method: 'POST', body: form, signal });
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error;
    const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    throw new DemoError(`No connection to the demo service. (${detail})`);
  }

  if (res.status === 429 || res.status === 503) {
    const seconds = Number(res.headers.get('retry-after') ?? '1');
    throw new Throttled(Math.max(seconds, 1) * 1000);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new DemoError(body?.message ?? 'Detection failed. Try again.');
  }
  return res.json();
}

export interface AuditLoginInput {
  name: string;
  place: string;
  company: string;
  selfieUri: string;
}

/** Records a login-screen visit (name/place/company + selfie) to the durable audit log.
 *  This is an info-capture step, not an access gate - the credential in front of it is
 *  shared/public, so this call is about who tested the app, not authorization. */
export async function submitAuditLogin(input: AuditLoginInput): Promise<void> {
  const form = new FormData();
  form.append('name', input.name);
  form.append('place', input.place);
  form.append('company', input.company);
  form.append('selfie', new File(input.selfieUri) as unknown as Blob, 'selfie.jpg');

  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/v1/demo/audit-login`, { method: 'POST', body: form });
  } catch (error) {
    const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    throw new DemoError(`No connection to the demo service. (${detail})`);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new DemoError(body?.message ?? 'Could not record your visit. Try again.');
  }
}

/** A frame pulled from the site's remote camera instead of the phone's own camera - the
 *  alternate Live Monitoring source. Not wired to a real camera yet (backend/demo_api
 *  returns 503 "not configured" until one exists); the shape is ready for when it is. */
export async function fetchRemoteFrame(signal?: AbortSignal): Promise<{ uri: string }> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/v1/demo/monitor/remote-frame`, { signal });
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error;
    const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    throw new DemoError(`No connection to the demo service. (${detail})`);
  }
  if (res.status === 429 || res.status === 503) {
    const seconds = Number(res.headers.get('retry-after') ?? '1');
    throw new Throttled(Math.max(seconds, 1) * 1000);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new DemoError(body?.message ?? 'The remote camera is not available right now.');
  }
  const blob = await res.blob();
  const file = new File(Paths.cache, `remote-frame-${Date.now()}.jpg`);
  file.write(new Uint8Array(await blob.arrayBuffer()));
  return { uri: file.uri };
}

export interface FaceDetection {
  box: NormBox;
  detectorConfidence: number;
  embedding: number[];
}

export interface DetectFaceResult {
  width: number;
  height: number;
  inference_ms: number;
  faces: FaceDetection[];
}

export async function detectFaces(imageUri: string, signal?: AbortSignal): Promise<DetectFaceResult> {
  const form = new FormData();
  // See the matching comment in detect() above - the old { uri, name, type } descriptor
  // no longer works with this SDK's fetch polyfill.
  form.append('image', new File(imageUri) as unknown as Blob, 'frame.jpg');

  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/v1/demo/detect-face`, { method: 'POST', body: form, signal });
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error;
    const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    throw new DemoError(`No connection to the demo service. (${detail})`);
  }

  if (res.status === 429 || res.status === 503) {
    const seconds = Number(res.headers.get('retry-after') ?? '1');
    throw new Throttled(Math.max(seconds, 1) * 1000);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new DemoError(body?.message ?? 'Face detection failed. Try again.');
  }
  const payload = await res.json();
  return {
    width: payload.width,
    height: payload.height,
    inference_ms: payload.inference_ms,
    faces: (payload.faces ?? []).map((f: { box: NormBox; detector_confidence: number; embedding: number[] }) => ({
      box: f.box,
      detectorConfidence: f.detector_confidence,
      embedding: f.embedding,
    })),
  };
}
