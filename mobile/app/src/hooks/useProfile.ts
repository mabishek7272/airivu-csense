import { useCallback, useEffect, useState } from 'react';
import { getMfaStatus, getProfile } from '../api/tenant';
import type { MfaStatusOut, ProfileOut } from '../api/types';
import { errorMessage } from '../utils/errorMessage';

export interface ProfileData {
  profile: ProfileOut;
  mfa: MfaStatusOut;
}

interface State {
  data: ProfileData | null;
  loading: boolean;
  error: string | null;
}

/** Fetches this user's own profile and MFA status together (`Promise.all`, run
 * concurrently) rather than as two independently-racing hooks - the Profile screen has
 * one loading/error state to branch on. `refresh()` is what the TOTP enroll flow calls
 * after a successful confirm so `mfa.enrolled`/`recovery_codes_remaining` reflect reality
 * immediately, matching the web CRM's own `mfa.reload()` in `SettingsPage.tsx`'s
 * `onEnrolled`. */
export function useProfile() {
  const [state, setState] = useState<State>({ data: null, loading: true, error: null });

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const [profile, mfa] = await Promise.all([getProfile(), getMfaStatus()]);
      setState({ data: { profile, mfa }, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: errorMessage(error) });
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  return { ...state, refresh: load };
}
