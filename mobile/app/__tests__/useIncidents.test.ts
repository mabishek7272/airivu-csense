import { act, renderHook, waitFor } from '@testing-library/react-native';
import { useIncidents } from '../src/hooks/useIncidents';
import * as tenantApi from '../src/api/tenant';
import type { IncidentStatusFilter, IncidentSummary } from '../src/api/types';

jest.mock('../src/api/tenant');

function summary(id: string): IncidentSummary {
  return {
    id,
    incident_number: Number(id),
    type_code: 'loitering',
    severity: 'high',
    status: 'open',
    title: `Incident ${id}`,
    summary: null,
    camera_id: 'cam-1',
    site_id: 'site-1',
    detection_count: 1,
    first_detected_at: '2026-08-01T00:00:00Z',
    last_detected_at: '2026-08-01T00:00:00Z',
    acknowledged_at: null,
  };
}

describe('useIncidents', () => {
  beforeEach(() => {
    jest.resetAllMocks();
  });

  it('loads the first page for the given status filter', async () => {
    (tenantApi.listIncidents as jest.Mock).mockResolvedValue({ items: [summary('1'), summary('2')], next_cursor: 'cursor-a' });

    const { result } = await renderHook(() => useIncidents('active'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.items).toHaveLength(2);
    expect(result.current.nextCursor).toBe('cursor-a');
    expect(tenantApi.listIncidents).toHaveBeenCalledWith({ status: 'active' });
  });

  it('appends the next page on loadMore, preserving already-loaded items', async () => {
    (tenantApi.listIncidents as jest.Mock)
      .mockResolvedValueOnce({ items: [summary('1')], next_cursor: 'cursor-a' })
      .mockResolvedValueOnce({ items: [summary('2')], next_cursor: null });

    const { result } = await renderHook(() => useIncidents('active'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.loadMore();
    });

    expect(result.current.items.map((i: IncidentSummary) => i.id)).toEqual(['1', '2']);
    expect(result.current.nextCursor).toBeNull();
    expect(tenantApi.listIncidents).toHaveBeenLastCalledWith({ status: 'active', cursor: 'cursor-a' });
  });

  it('does nothing on loadMore once nextCursor is exhausted', async () => {
    (tenantApi.listIncidents as jest.Mock).mockResolvedValue({ items: [summary('1')], next_cursor: null });

    const { result } = await renderHook(() => useIncidents('active'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.loadMore();
    });

    expect(tenantApi.listIncidents).toHaveBeenCalledTimes(1);
  });

  it('resets the list and issues a fresh query when the status filter changes', async () => {
    (tenantApi.listIncidents as jest.Mock)
      .mockResolvedValueOnce({ items: [summary('1')], next_cursor: null })
      .mockResolvedValueOnce({ items: [summary('9')], next_cursor: null });

    const { result, rerender } = await renderHook(
      ({ status }: { status: IncidentStatusFilter }) => useIncidents(status),
      { initialProps: { status: 'active' as IncidentStatusFilter } },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.items.map((i: IncidentSummary) => i.id)).toEqual(['1']);

    await rerender({ status: 'resolved' as IncidentStatusFilter });
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.items.map((i: IncidentSummary) => i.id)).toEqual(['9']);
    expect(tenantApi.listIncidents).toHaveBeenLastCalledWith({ status: 'resolved' });
  });
});
