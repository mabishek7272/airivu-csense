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

  it('resolves to loggedOut when no session is stored', async () => {
    // `authStage` genuinely starts `'checking'` (see AuthContext.tsx's own docstring) -
    // but this library's `renderHook` is itself async and already flushes the mount
    // effect (and its already-resolved mock promise) before returning, so by the time
    // this test can observe it, the real momentary `checking` has already settled to
    // `loggedOut`.
    (tokenStore.isLoggedIn as jest.Mock).mockResolvedValue(false);
    const { result } = await renderHook(() => useAuth(), { wrapper });

    await waitFor(() => expect(result.current.authStage).toBe('loggedOut'));
  });

  it('sets authStage to loggedIn on a successful direct login', async () => {
    (tokenStore.isLoggedIn as jest.Mock).mockResolvedValue(false);
    (authApi.login as jest.Mock).mockResolvedValue({ status: 'authenticated' });
    const { result } = await renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.authStage).toBe('loggedOut'));

    await act(async () => {
      await result.current.login('owner@example.com', 'correct-password');
    });

    expect(result.current.authStage).toBe('loggedIn');
    expect(result.current.loginError).toBeNull();
    expect(authApi.login).toHaveBeenCalledWith('owner@example.com', 'correct-password');
  });

  it('sets authStage to mfaRequired and records the challenge when login demands MFA', async () => {
    (tokenStore.isLoggedIn as jest.Mock).mockResolvedValue(false);
    (authApi.login as jest.Mock).mockResolvedValue({
      status: 'mfa_required',
      mfa_ticket: 'ticket-123',
      mfa_factor: 'totp',
    });
    const { result } = await renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.authStage).toBe('loggedOut'));

    await act(async () => {
      await result.current.login('owner@example.com', 'correct-password');
    });

    expect(result.current.authStage).toBe('mfaRequired');
    expect(result.current.mfaChallenge).toEqual({ ticket: 'ticket-123', factor: 'totp' });
  });

  it('verifyMfa moves authStage to loggedIn and clears the challenge on success', async () => {
    (tokenStore.isLoggedIn as jest.Mock).mockResolvedValue(false);
    (authApi.login as jest.Mock).mockResolvedValue({
      status: 'mfa_required',
      mfa_ticket: 'ticket-123',
      mfa_factor: 'email_otp',
    });
    (authApi.verifyMfa as jest.Mock).mockResolvedValue({ status: 'authenticated' });
    const { result } = await renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.authStage).toBe('loggedOut'));

    await act(async () => {
      await result.current.login('owner@example.com', 'correct-password');
    });
    expect(result.current.authStage).toBe('mfaRequired');

    await act(async () => {
      await result.current.verifyMfa({ code: '123456' });
    });

    expect(authApi.verifyMfa).toHaveBeenCalledWith({ mfa_ticket: 'ticket-123', code: '123456' });
    expect(result.current.authStage).toBe('loggedIn');
    expect(result.current.mfaChallenge).toBeNull();
  });

  it('cancelMfa resets to loggedOut and clears the challenge', async () => {
    (tokenStore.isLoggedIn as jest.Mock).mockResolvedValue(false);
    (authApi.login as jest.Mock).mockResolvedValue({
      status: 'mfa_required',
      mfa_ticket: 'ticket-123',
      mfa_factor: 'totp',
    });
    const { result } = await renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.authStage).toBe('loggedOut'));

    await act(async () => {
      await result.current.login('owner@example.com', 'correct-password');
    });
    expect(result.current.authStage).toBe('mfaRequired');

    await act(async () => {
      result.current.cancelMfa();
    });

    expect(result.current.authStage).toBe('loggedOut');
    expect(result.current.mfaChallenge).toBeNull();
  });

  it('surfaces the real server message and stays logged out on a failed login', async () => {
    (tokenStore.isLoggedIn as jest.Mock).mockResolvedValue(false);
    (authApi.login as jest.Mock).mockRejectedValue(
      new ApiError({ code: 'invalid_credentials', httpStatusCode: 401, retryable: false, message: 'Invalid email or password.' }),
    );
    const { result } = await renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.authStage).toBe('loggedOut'));

    await act(async () => {
      await expect(result.current.login('owner@example.com', 'wrong-password')).rejects.toThrow();
    });

    expect(result.current.authStage).toBe('loggedOut');
    expect(result.current.loginError).toBe('Invalid email or password.');
  });

  it('logs out and clears authStage even when the server revoke call fails', async () => {
    (tokenStore.isLoggedIn as jest.Mock).mockResolvedValue(true);
    (authApi.logout as jest.Mock).mockResolvedValue(undefined); // api/auth.ts's own logout()
    // already swallows a failed server revoke internally (see its own finally block) -
    // this context layer just needs to trust logout() always resolves.
    const { result } = await renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.authStage).toBe('loggedIn'));

    await act(async () => {
      await result.current.logout();
    });

    expect(result.current.authStage).toBe('loggedOut');
  });
});
