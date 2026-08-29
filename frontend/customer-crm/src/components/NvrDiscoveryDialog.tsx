import { useMemo, useState } from "react";
import { createCamera } from "../api/cameras";
import { listSites } from "../api/sites";
import { discoverNvrChannels, type NvrChannel } from "../api/nvr";
import { ApiRequestError } from "../api/client";
import { Dialog } from "./Dialog";
import { InlineSpinner } from "./States";
import { useNotifications } from "./Notifications";
import { useResource } from "../hooks/useResource";

/** Discover an NVR's channels and turn selected ones into real cameras.
 *
 *  The one reference adapter behind this is mock (`api/nvr.ts`'s own docstring) - it never
 *  opens a real connection, so every returned channel is clearly labelled `Mock NVR`
 *  rather than left to look like a real scan.
 */
export function NvrDiscoveryDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const notify = useNotifications();
  const sites = useResource(listSites, []);
  const siteOptions = useMemo(() => sites.data ?? [], [sites.data]);

  const [siteId, setSiteId] = useState("");
  const [hostname, setHostname] = useState("");
  const [port, setPort] = useState("80");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const [discovering, setDiscovering] = useState(false);
  const [discoverError, setDiscoverError] = useState<unknown>(null);
  const [channels, setChannels] = useState<NvrChannel[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);

  async function handleDiscover() {
    setDiscovering(true);
    setDiscoverError(null);
    try {
      const result = await discoverNvrChannels({
        hostname, port: Number(port) || 80, username, password,
      });
      setChannels(result.channels);
      setSelected(new Set(result.channels.map((c) => c.channel_id)));
    } catch (err) {
      setDiscoverError(err);
    } finally {
      setDiscovering(false);
    }
  }

  function toggle(channelId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(channelId)) next.delete(channelId);
      else next.add(channelId);
      return next;
    });
  }

  async function handleAddSelected() {
    if (!channels || !siteId) return;
    setAdding(true);
    try {
      const toAdd = channels.filter((c) => selected.has(c.channel_id));
      for (const channel of toAdd) {
        await createCamera({
          site_id: siteId,
          name: channel.name,
          code: `nvr-${hostname.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}-${channel.channel_id}`,
          vendor: channel.vendor ?? undefined,
          model: channel.model ?? undefined,
          hostname,
          rtsp_port: Number(port) || 80,
          main_stream_path: channel.main_stream_path,
          sub_stream_path: channel.sub_stream_path ?? undefined,
        });
      }
      notify.success(
        `${toAdd.length} camera${toAdd.length === 1 ? "" : "s"} added from this NVR`,
        "Set each one's credential separately before probing it.",
      );
      onCreated();
    } catch (err) {
      notify.error(
        "Could not add the selected channels",
        err instanceof Error ? err.message : undefined,
      );
    } finally {
      setAdding(false);
    }
  }

  return (
    <Dialog open title="Discover cameras from an NVR" onClose={onClose}>
      {!channels && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <label>
            Site
            <select value={siteId} onChange={(e) => setSiteId(e.target.value)}>
              <option value="">Choose a site…</option>
              {siteOptions.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.timezone})
                </option>
              ))}
            </select>
          </label>
          <label>
            NVR hostname or address
            <input value={hostname} onChange={(e) => setHostname(e.target.value)} />
          </label>
          <label>
            Port
            <input value={port} onChange={(e) => setPort(e.target.value)} />
          </label>
          <label>
            Username
            <input value={username} onChange={(e) => setUsername(e.target.value)} />
          </label>
          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>

          {discovering && <InlineSpinner label="Discovering channels…" />}
          {!discovering && discoverError !== null && (
            <p role="alert" className="error-panel">
              {discoverError instanceof ApiRequestError
                ? discoverError.body.message
                : "Could not discover this NVR's channels."}
            </p>
          )}

          <div className="form-actions">
            <button
              type="button"
              className="primary"
              disabled={!siteId || !hostname || !username || !password || discovering}
              onClick={() => void handleDiscover()}
            >
              Discover channels
            </button>
          </div>
        </div>
      )}

      {channels && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p className="muted">
            {channels.length} channel{channels.length === 1 ? "" : "s"} found. Pick which
            ones to add as cameras.
          </p>
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">
                  <span className="visually-hidden">Selected</span>
                </th>
                <th scope="col">Channel</th>
                <th scope="col">Vendor / model</th>
                <th scope="col">Main stream</th>
              </tr>
            </thead>
            <tbody>
              {channels.map((channel) => (
                <tr key={channel.channel_id}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(channel.channel_id)}
                      onChange={() => toggle(channel.channel_id)}
                      aria-label={`Add ${channel.name}`}
                    />
                  </td>
                  <td>{channel.name}</td>
                  <td className="muted">
                    {channel.vendor} {channel.model}
                  </td>
                  <td className="mono">{channel.main_stream_path}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="form-actions">
            <button type="button" className="btn-quiet" onClick={() => setChannels(null)}>
              Back
            </button>
            <button
              type="button"
              className="primary"
              disabled={selected.size === 0 || adding}
              onClick={() => void handleAddSelected()}
            >
              {adding ? "Adding…" : `Add ${selected.size} selected as camera${selected.size === 1 ? "" : "s"}`}
            </button>
          </div>
        </div>
      )}
    </Dialog>
  );
}
