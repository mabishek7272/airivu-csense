import { Redirect } from 'expo-router';
import { useAuth } from '../src/auth/AuthContext';
import { LoadingState } from '../src/components/States';

/**
 * Bare "/" has no route of its own - `_layout.tsx`'s root Stack only declares `(tabs)`
 * and `login` as named screens (each gated by `Stack.Protected`), neither of which
 * matches an empty path. Without this file, opening the app cold (or any deep link with
 * no path, e.g. `csense:///`) renders expo-router's own "Unmatched Route" screen instead
 * of the app - found by actually launching the app on a real iOS Simulator build, not by
 * inspection (see mobile/app/README.md's own "What's been verified" section - this file
 * didn't exist because the app had never been run before this).
 *
 * **This redirect must follow the same `isLoggedIn` state `_layout.tsx` gates on, not a
 * fixed target.** An unconditional `<Redirect href="/(tabs)/dashboard" />` was tried
 * first and hung the app on a real device: `Stack.Protected guard={isLoggedIn}` removes
 * `(tabs)` from the navigator entirely while logged out, and expo-router doesn't fall
 * back to the visible branch on its own - it keeps re-attempting to resolve a route that
 * doesn't exist, which reads as a silent infinite loop, not an error. Redirecting to
 * `/login` while logged out (mirroring `_layout.tsx`'s own branch, and showing the same
 * loading state during the initial SecureStore check) avoids ever targeting a route the
 * guard has hidden.
 */
export default function Index() {
  const { isLoggedIn } = useAuth();

  if (isLoggedIn === null) {
    return <LoadingState label="Starting up…" />;
  }

  return <Redirect href={isLoggedIn ? '/(tabs)/dashboard' : '/login'} />;
}
