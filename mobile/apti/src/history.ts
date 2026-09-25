import AsyncStorage from '@react-native-async-storage/async-storage';
import { Directory, File, Paths } from 'expo-file-system';

export type HistorySource = 'detect' | 'monitor';
export type Severity = 'low' | 'medium' | 'high';

export interface HistoryEntry {
  id: string;
  source: HistorySource;
  label: string;
  confidence: number | null;
  severity: Severity;
  timestamp: number;
  /** A local file:// path only - never uploaded, never synced. */
  thumbnailUri: string | null;
}

export const MAX_HISTORY_ENTRIES = 200;
const STORAGE_KEY = 'apti.history.v1';
const historyDir = new Directory(Paths.document, 'apti-history');

/** Pure: applies the cap, returning what to keep and what was dropped (so the caller can
 *  clean up dropped thumbnails). Newest-first ordering by `timestamp`. */
export function applyCap(
  entries: HistoryEntry[],
  max: number = MAX_HISTORY_ENTRIES,
): { kept: HistoryEntry[]; dropped: HistoryEntry[] } {
  if (entries.length <= max) return { kept: entries, dropped: [] };
  const sorted = [...entries].sort((a, b) => b.timestamp - a.timestamp);
  return { kept: sorted.slice(0, max), dropped: sorted.slice(max) };
}

export async function loadHistory(): Promise<HistoryEntry[]> {
  const raw = await AsyncStorage.getItem(STORAGE_KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

async function saveHistory(entries: HistoryEntry[]): Promise<void> {
  await AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
}

/** Copies a temporary camera/picker file into this app's own document directory under a
 *  name tied to this entry, so it survives after the OS cleans up the source's temp file -
 *  still entirely local to the device. A failure here is not fatal; the entry still saves
 *  without a thumbnail. */
async function saveThumbnail(id: string, sourceUri: string): Promise<string | null> {
  try {
    if (!historyDir.exists) historyDir.create({ intermediates: true });
    const source = new File(sourceUri);
    const dest = new File(historyDir, `${id}.jpg`);
    await source.copy(dest);
    return dest.uri;
  } catch {
    return null;
  }
}

function deleteThumbnail(uri: string | null): void {
  if (!uri) return;
  try {
    const file = new File(uri);
    if (file.exists) file.delete();
  } catch {
    // best-effort cleanup
  }
}

export async function addHistoryEntry(input: {
  source: HistorySource;
  label: string;
  confidence: number | null;
  severity: Severity;
  /** A local frame/photo uri to copy in as this entry's thumbnail, if any. */
  thumbnailSourceUri?: string;
}): Promise<HistoryEntry> {
  const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const thumbnailUri = input.thumbnailSourceUri ? await saveThumbnail(id, input.thumbnailSourceUri) : null;
  const entry: HistoryEntry = {
    id,
    source: input.source,
    label: input.label,
    confidence: input.confidence,
    severity: input.severity,
    timestamp: Date.now(),
    thumbnailUri,
  };
  const current = await loadHistory();
  const { kept, dropped } = applyCap([entry, ...current]);
  dropped.forEach((d) => deleteThumbnail(d.thumbnailUri));
  await saveHistory(kept);
  return entry;
}

export async function clearHistory(): Promise<void> {
  const current = await loadHistory();
  current.forEach((e) => deleteThumbnail(e.thumbnailUri));
  await AsyncStorage.removeItem(STORAGE_KEY);
}
