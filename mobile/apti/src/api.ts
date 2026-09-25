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
  // React Native's FormData accepts a { uri, name, type } descriptor for a file.
  form.append('image', { uri: imageUri, name: 'frame.jpg', type: 'image/jpeg' } as unknown as Blob);

  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/v1/demo/detect`, { method: 'POST', body: form, signal });
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error;
    throw new DemoError('No connection to the demo service.');
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
  form.append('image', { uri: imageUri, name: 'frame.jpg', type: 'image/jpeg' } as unknown as Blob);

  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/v1/demo/detect-face`, { method: 'POST', body: form, signal });
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error;
    throw new DemoError('No connection to the demo service.');
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
