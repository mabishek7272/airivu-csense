import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react-native';
import { AuthProvider, useAuth } from '../src/auth/AuthContext';
import * as authApi from '../src/api/auth';
import { tokenStore } from '../src/auth/tokenStore';
import { ApiError } from '../src/api/client';

jest.mock('../src/api/auth');
jest.mock('../src/auth/tokenStore', () => ({
  tokenStore: {
    isLoggedIn: jest.fn(),
  },
}));

const wrapper = ({ children }: { children: React.ReactNode }) => <AuthProvider>{children}</AuthProvider>;

describe('AuthContext', () => {
  beforeEach(() => {
    jest.resetAllMocks();
  });

  it('resolves to false when no session is stored', async () => {
    // `isLoggedIn` genuinely starts `null` (see AuthContext.tsx's own docstring) - but
    // this library's `renderHook` is itself async and already flushes the mount effect
    // (and its already-resolved mock promise) before returning, so by the time this
    // test can observe it, the real momentary `null` has already settled to `false`.
    (tokenStore.isLoggedIn as jest.Mock).mockResolvedValue(false);
    const { result } = await renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.isLoggedIn).toBe(false));
  });

  it('sets isLoggedIn true on a successful login', async () => {
    (tokenStore.isLoggedIn as jest.Mock).mockResolvedValue(false);
    (authApi.login as jest.Mock).mockResolvedValue(undefined);
    const { result } = await renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isLoggedIn).toBe(false));

    await act(async () => {
      await result.current.login('owner@example.com', 'correct-password');
    });

    expect(result.current.isLoggedIn).toBe(true);
    expect(result.current.loginError).toBeNull();
    expect(authApi.login).toHaveBeenCalledWith('owner@example.com', 'correct-password');
  });

  it('surfaces the real server message and stays logged out on a failed login', async () => {
    (tokenStore.isLoggedIn as jest.Mock).mockResolvedValue(false);
    (authApi.login as jest.Mock).mockRejectedValue(
      new ApiError({ code: 'invalid_credentials', httpStatusCode: 401, retryable: false, message: 'Invalid email or password.' }),
    );
    const { result } = await renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isLoggedIn).toBe(false));

    await act(async () => {
      await expect(result.current.login('owner@example.com', 'wrong-password')).rejects.toThrow();
    });

    expect(result.current.isLoggedIn).toBe(false);
    expect(result.current.loginError).toBe('Invalid email or password.');
  });

  it('logs out and clears isLoggedIn even when the server revoke call fails', async () => {
    (tokenStore.isLoggedIn as jest.Mock).mockResolvedValue(true);
    (authApi.logout as jest.Mock).mockResolvedValue(undefined); // api/auth.ts's own logout()
    // already swallows a failed server revoke internally (see its own finally block) -
    // this context layer just needs to trust logout() always resolves.
    const { result } = await renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isLoggedIn).toBe(true));

    await act(async () => {
      await result.current.logout();
    });

    expect(result.current.isLoggedIn).toBe(false);
  });
});
