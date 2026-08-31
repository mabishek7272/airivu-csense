import { apiRequest, ApiError, NetworkError } from '../src/api/client';
import { tokenStore } from '../src/auth/tokenStore';

jest.mock('../src/auth/tokenStore', () => ({
  tokenStore: {
    getAccessToken: jest.fn(),
    setSession: jest.fn(),
    clear: jest.fn(),
  },
}));

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'status',
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

describe('apiRequest', () => {
  beforeEach(() => {
    jest.resetAllMocks();
    (tokenStore.getAccessToken as jest.Mock).mockResolvedValue('token-123');
  });

  it('returns parsed JSON on a real 200', async () => {
    global.fetch = jest.fn().mockResolvedValue(jsonResponse(200, { hello: 'world' }));
    const result = await apiRequest<{ hello: string }>('/api/v1/tenant/dashboard');
    expect(result).toEqual({ hello: 'world' });
  });

  it('throws ApiError with the server-provided problem shape on a non-2xx', async () => {
    global.fetch = jest.fn().mockResolvedValue(
      jsonResponse(403, { code: 'forbidden', message: 'Not allowed', retryable: false }),
    );
    await expect(apiRequest('/api/v1/tenant/cameras')).rejects.toMatchObject({
      code: 'forbidden',
      httpStatusCode: 403,
      message: 'Not allowed',
    });
  });

  it('wraps a thrown fetch failure as NetworkError', async () => {
    global.fetch = jest.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(apiRequest('/api/v1/tenant/dashboard')).rejects.toBeInstanceOf(NetworkError);
  });

  it('refreshes once and retries on a real 401, then succeeds', async () => {
    const fetchMock = jest
      .fn()
      // 1: original request -> 401
      .mockResolvedValueOnce(jsonResponse(401, { code: 'unauthorized', message: 'Expired', retryable: false }))
      // 2: refresh call -> 200 with a fresh token
      .mockResolvedValueOnce(jsonResponse(200, { access_token: 'new-token', token_type: 'bearer', expires_in: 900, tenant_id: 't1' }))
      // 3: retried original request -> 200
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    global.fetch = fetchMock;

    const result = await apiRequest<{ ok: boolean }>('/api/v1/tenant/dashboard');

    expect(result).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(tokenStore.setSession).toHaveBeenCalledWith({
      accessToken: 'new-token',
      expiresInSeconds: 900,
      tenantId: 't1',
    });
  });

  it('clears the local session and surfaces the real 401 when refresh itself fails', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse(401, { code: 'unauthorized', message: 'Expired', retryable: false }))
      .mockResolvedValueOnce(jsonResponse(401, { code: 'unauthorized', message: 'Refresh expired', retryable: false }));
    global.fetch = fetchMock;

    await expect(apiRequest('/api/v1/tenant/dashboard')).rejects.toBeInstanceOf(ApiError);
    expect(tokenStore.clear).toHaveBeenCalled();
    // No third call - a failed refresh does not retry the original request again.
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('shares one in-flight refresh across two concurrent 401s', async () => {
    // Both parallel requests see a 401 before the shared refresh resolves - modeled as
    // an explicit response queue (call order), since real timing is what's under test.
    const responses = [
      jsonResponse(401, { code: 'unauthorized', message: 'Expired', retryable: false }),
      jsonResponse(401, { code: 'unauthorized', message: 'Expired', retryable: false }),
      jsonResponse(200, { access_token: 'new-token', token_type: 'bearer', expires_in: 900, tenant_id: 't1' }),
      jsonResponse(200, { ok: 1 }),
      jsonResponse(200, { ok: 2 }),
    ];
    global.fetch = jest.fn().mockImplementation(() => Promise.resolve(responses.shift()));

    const [a, b] = await Promise.all([
      apiRequest('/api/v1/tenant/dashboard'),
      apiRequest('/api/v1/tenant/cameras'),
    ]);

    expect(a).toEqual({ ok: 1 });
    expect(b).toEqual({ ok: 2 });
    // Exactly one refresh call total (5 fetches: 2 x 401, 1 refresh, 2 retries).
    expect((global.fetch as jest.Mock).mock.calls.filter((c) => String(c[0]).includes('/auth/refresh')).length).toBe(1);
  });
});
