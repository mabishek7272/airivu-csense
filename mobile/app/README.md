# CSense Mobile

One Expo/React Native (TypeScript) codebase covering both iOS and Android - the tenant
app for operators to check their dashboard, work incidents, and check camera/license
health from a phone. This replaced an earlier native-Android-only build (Kotlin/Jetpack
Compose, see git history) once it turned out the team already had Expo tooling in place;
the backend-contract reverse-engineering from that build (cookie-based refresh, the exact
DTO shapes) carried forward into this rewrite.

## Stack

- Expo SDK 57, React 19.2.3, React Native 0.86.3, TypeScript 6.0.3
- [Expo Router](https://docs.expo.dev/router/introduction/) for file-based navigation
  (`app/`), gated by `Stack.Protected` on a real auth state
- `expo-secure-store` for the access token (Keychain on iOS, EncryptedSharedPreferences
  on Android) - never AsyncStorage, which is unencrypted
- `@preeternal/react-native-cookie-manager` for exactly one thing: reading the
  `csense_session` cookie's current value for the logout endpoint's session-id path
  parameter (see `src/auth/cookies.ts`'s own docstring for why nothing else touches
  cookies directly)

## Project layout

```
app/                  Expo Router screens (file-based routing)
  _layout.tsx          Root layout - AuthProvider + Stack.Protected auth gate
  login.tsx
  (tabs)/              Tab navigator: dashboard, incidents, cameras, account
    incidents/          Its own Stack: list (index.tsx) -> detail ([id].tsx)
src/
  api/                 HTTP client, DTO types, one file per backend resource
  auth/                AuthContext, SecureStore-backed token storage, cookie helper
  components/          Shared UI: severity/status badges, loading/error/empty states
  hooks/               One data-fetching hook per screen's resource
  theme/               Colour tokens - copied hex-for-hex from the web Customer CRM's
                        own stylesheet, so severity/status colours mean the same thing
                        on both clients
  utils/
__tests__/             Jest unit tests (see "Testing" below)
```

## Backend contract

Every type in `src/api/types.ts` and every request in `src/api/*.ts` was read directly
from the real backend (`backend/tenant_api/app/api/*.py`), not guessed or inferred from
documentation. Two contract details worth knowing before touching the auth layer:

- **Login never returns a refresh token in the response body.** The backend sets
  `csense_session`/`csense_refresh` as httpOnly cookies, path-scoped to
  `/api/v1/auth`. This app relies on the *platform's own* native cookie jar
  (`NSURLSession` on iOS, `OkHttp` on Android) to store and resend those cookies
  transparently on every `fetch()` call - the same way a browser does for the web CRM.
  React Native's own `fetch()` does not reliably expose `Set-Cookie` response headers to
  JS, so nothing in this app parses or sets cookies by hand.
- **Logout needs the session id from a cookie, not the API response.**
  `DELETE /api/v1/auth/sessions/{sessionId}` is how a session is actually revoked
  server-side; the id comes from reading the `csense_session` cookie via
  `CookieManager.get()` (see `src/auth/cookies.ts`). Local session data
  (`src/auth/tokenStore.ts`) is always cleared regardless of whether that call succeeds
  - a user tapping "sign out" while offline must still actually be signed out on this
  device.

## Running locally

```sh
npm install
npx expo start
```

Then press `a` for an Android emulator or `i` for an iOS Simulator (Simulator only -
building for a physical iOS device needs a paid Apple Developer account and a real
provisioning profile, neither of which this repo can supply). `npm run android` /
`npm run ios` are shortcuts for the same thing.

### Pointing at a backend

By default the app targets this repo's own local dev stack
(`docker compose up`, see the repo root README):

- Android emulator: `http://10.0.2.2:8080` (the emulator's alias for the host machine)
- iOS Simulator: `http://localhost:8080` (the Simulator shares the host's own network
  stack directly)

Override with a `.env` file (gitignored) at this directory's root:

```
EXPO_PUBLIC_API_BASE_URL=https://your-tenant.example.com
EXPO_PUBLIC_API_HOST=your-tenant.example.com
```

`EXPO_PUBLIC_API_HOST` sets the `Host` header Traefik's own routing needs
(`infra/traefik/dynamic.yml`) when not talking to a real DNS-resolved domain; against a
real production domain it's a harmless no-op once set to that same domain. A **physical**
device on either platform needs your dev machine's real LAN IP in
`EXPO_PUBLIC_API_BASE_URL` instead of either emulator default.

## Testing

```sh
npm run lint        # eslint (eslint-config-expo + react-hooks)
npm run typecheck   # tsc --noEmit
npm test            # jest (jest-expo preset)
```

All three are real and pass as of this writing (17 Jest tests: the `ApiError`/
`NetworkError` mapping and 401-refresh-and-retry contract in `src/api/client.ts`, login/
logout state transitions in `AuthContext`, and cursor-pagination/status-filter-reset
behavior in `useIncidents`). CI runs the same three commands on every push
(`.github/workflows/ci.yml`'s `mobile-app` job).

## What's verified and what isn't

This app has been lint-clean, typecheck-clean, and unit-test-green from the start, and
its every request/response shape was read from the real backend source, not guessed.
What it has **not** yet been run against is a real device or a real Metro bundler session
against a live backend - CI has no macOS runner for an iOS build and no real
dev-client/EAS build step for Android, so `npm test`'s Node-based Jest run is the deepest
verification this repo's own CI can give it. Before a real release:

- Run `npx expo start` against a live local backend and walk the login → dashboard →
  incidents → detail → resolve/dismiss → logout flow on both an Android emulator and an
  iOS Simulator.
- Confirm the native-cookie-jar assumption for real (see `src/auth/cookies.ts`) - that
  `fetch()` actually resends `csense_session`/`csense_refresh` without any explicit
  cookie handling in this app's own code, on both platforms.
- Set up EAS Build (or a local Xcode/Android Studio build) for real signed binaries -
  this repo only takes the app as far as `expo start`.

## App identity

- Name: CSense · Bundle/package id: `ai.airivu.csense` (both platforms)
- No icons/splash screens have been customized yet - `assets/` still holds
  `create-expo-app`'s template placeholders.
