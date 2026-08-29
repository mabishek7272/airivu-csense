import { apiFetch } from "./client";

/** NVR channel discovery. The backend's one reference adapter is deliberately mock - it
 *  never opens a real connection - which is why every channel it returns carries
 *  `vendor: "Mock NVR"` rather than trying to look like a real integration.
 */

export interface NvrChannel {
  channel_id: string;
  name: string;
  main_stream_path: string;
  sub_stream_path: string | null;
  vendor: string | null;
  model: string | null;
}

export interface NvrDiscoverInput {
  hostname: string;
  port?: number;
  username: string;
  password: string;
  edge_device_id?: string;
}

export function discoverNvrChannels(body: NvrDiscoverInput) {
  return apiFetch<{ channels: NvrChannel[] }>("/api/v1/tenant/nvr/discover", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
