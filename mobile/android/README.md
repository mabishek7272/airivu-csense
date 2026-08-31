# CSense — Android (native)

A native Kotlin/Jetpack Compose companion app for AIRIVU CSense - login, dashboard,
incident list/detail with real acknowledge/investigate/resolve/dismiss actions, camera
list, and account/license status. Talks to the same real Tenant API
(`docs/08_API_GUIDE.md`) every other client in this repo talks to - no separate mock
backend, no simulated data.

**Package**: `ai.airivu.csense`. **Min SDK**: 26 (Android 8.0). **Target/compile SDK**: 36.

## Why native, not Flutter/React Native

This repo already has Flutter tooling on the development machine, but the app was built
plain-native (Kotlin + Jetpack Compose for Android; Swift/SwiftUI is the equivalent,
not-yet-built iOS counterpart - see "iOS" below) because that's what was actually asked
for: a proprietary native app per platform, not a cross-platform framework wrapping a
shared codebase. The real cost of that choice is named, not hidden: the Android and iOS
apps will be two separate codebases with their own UI layers, sharing nothing but the
API contract they both talk to.

## Architecture

```
app/src/main/kotlin/ai/airivu/csense/
  CSenseApplication.kt, AppContainer.kt   - manual DI container (see below)
  MainActivity.kt                          - NavHost + bottom nav, the app's one Activity
  data/
    auth/       TokenStore (EncryptedSharedPreferences), PersistentCookieJar (real,
                encrypted, persistent - the mobile equivalent of a browser's cookie jar,
                since the backend's refresh flow uses an httpOnly cookie, not a JSON field)
    network/    ApiClient, AuthApiService (login/refresh, unauthenticated client),
                TenantApiService (everything else, authenticated + auto-refresh-on-401),
                SessionAuthenticator, SafeApiCall (maps every real backend error into a
                typed ApiException), dto/ (every DTO mirrors a real backend Pydantic model
                field-for-field - see each file's own docstring for which one)
    repository/ One repository per real API surface (auth/dashboard/incidents/cameras/
                license) - thin wrappers around TenantApiService + safeApiCall
  ui/
    theme/, navigation/, common/            - shared building blocks
    login/, dashboard/, incidents/, cameras/, account/   - one package per screen,
                each with a ViewModel (real StateFlow-based UI state) and a Composable
```

**Manual DI, not Hilt.** `AppContainer` is a small, hand-written singleton graph. Real
tradeoff, named rather than defaulted-into: Hilt's annotation processing adds real build
time and memory pressure, and this app's dependency graph is small enough (one token
store, one cookie jar, one API client, five repositories) that a manual container is
just as correct and considerably cheaper to build - a real, deliberate choice for the
memory-constrained development machine this was built on (see "Development machine
constraints" below), not a permanent architectural stance.

## Real backend contract, not guessed

Every DTO in `data/network/dto/` was written by reading the actual backend Pydantic
models (`backend/tenant_api/app/api/*.py`) field-for-field - not inferred from
documentation, which can drift. Two contract details worth knowing before touching the
auth code:

- **The refresh token is never in a JSON response body.** `POST /api/v1/auth/login` sets
  `csense_session`/`csense_refresh` as httpOnly cookies, path-scoped to `/api/v1/auth`
  (`backend/tenant_api/app/api/auth.py`). This app's `PersistentCookieJar` is a real,
  encrypted, persistent OkHttp `CookieJar` - without it, refresh would work once per
  process lifetime and then silently stop.
- **Logout needs a session id the JSON never provides either** - `DELETE
  /api/v1/auth/sessions/{sessionId}` reads it from the same cookie
  (`PersistentCookieJar.findCookieValue("csense_session")`).

## Building and running

Requires: JDK 17+ (this project was built and verified against Android Studio's own
bundled JBR, JDK 21 - `"$ANDROID_STUDIO_HOME/jbr"`), the Android SDK (compileSdk 36,
`build-tools;36.1.0`).

```bash
cd mobile/android
echo 'sdk.dir=/path/to/Android/Sdk' > local.properties   # machine-specific, gitignored -
    # on Windows, both the drive-letter colon AND every backslash must be escaped in a
    # Java .properties file, e.g. sdk.dir=C\:\\Users\\you\\AppData\\Local\\Android\\Sdk -
    # an unescaped colon after the drive letter is silently read as the key/value
    # separator, truncating the path and producing a confusing AGP SDK-validation error.

JAVA_HOME=/path/to/jdk17-or-newer ./gradlew :app:assembleDebug   # -> app/build/outputs/apk/debug/app-debug.apk
JAVA_HOME=/path/to/jdk17-or-newer ./gradlew :app:testDebugUnitTest
```

**Pointing at a real backend**: two Gradle properties, both baked into `BuildConfig` at
build time (`app/build.gradle.kts`):

```bash
./gradlew :app:assembleDebug -PapiBaseUrl=https://your-real-domain -PapiHost=your-real-domain
```

The defaults (`http://10.0.2.2:8080` / `app.localhost`) target this repo's own local dev
stack from an Android emulator (`10.0.2.2` is the emulator's own alias for the host
machine's `localhost`) - the same stack every backend e2e script in this repo already
runs against, with the same `Host: app.localhost` header Traefik's own routing needs
(`infra/traefik/dynamic.yml`) added automatically (`HostHeaderInterceptor`). Point a real
device (not an emulator) at a dev machine on the same network by using that machine's LAN
IP instead of `10.0.2.2`; point either at production
(`docs/10_PRODUCTION_DEPLOYMENT_GUIDE.md`) with a real `https://` domain - the cleartext
carve-out (`network_security_config.xml`) only ever applies to `10.0.2.2`/`localhost`/
`127.0.0.1`, so a real domain gets full, unexceptional TLS enforcement.

## What's been verified for real, and what hasn't

- **Compiles for real**: `:app:compileDebugKotlin` - clean, zero warnings.
- **Builds a real, installable APK for real**: `:app:assembleDebug` - a genuine 19MB
  `app-debug.apk`, not a stub.
- **12 real unit tests, all passing** (`:app:testDebugUnitTest`): login validation and
  both success/failure paths (`LoginViewModelTest`), cursor-pagination `loadMore`
  correctly appending rather than replacing and not over-fetching once `next_cursor` is
  null, status-filter changes triggering a fresh (non-appending) reload
  (`IncidentListViewModelTest`), and the real error-mapping contract - a genuine
  `ProblemResponse` JSON body parsed into the right `ApiException.Api` code/message/
  retryable fields, a malformed body still producing a usable error rather than a crash,
  and a real `IOException` mapping to `ApiException.Network` (`SafeApiCallTest`).
- **Not yet run on a real device or emulator.** This was built on a 12GB development
  machine that hit genuine, repeated OS-level out-of-memory conditions running this
  session's own Docker stack alone (see the main repo's own session notes) - an Android
  emulator needs ~2GB+ of its own RAM on top of that, and attempting it at the point this
  was built risked the same class of crash already seen repeatedly. **This is a real,
  named boundary, not a skipped step**: install the real APK
  (`app/build/outputs/apk/debug/app-debug.apk`) on a real device via `adb install`, or
  launch the AVD already configured on this machine (`Medium_Phone`) once there's enough
  free memory, to complete interactive verification against the real running backend.

## iOS

Not built. Native iOS (Swift/SwiftUI) needs Xcode, which needs macOS - genuinely
unavailable on this Windows development machine, the same hard platform constraint this
repo already names for anything requiring hardware or an OS this environment doesn't
have (GPU inference, real edge hardware). The real backend contract this Android app
already reverse-engineered field-for-field (`data/network/dto/`) is the same one an iOS
app would need - that mapping work does not need redoing, only re-expressing in Swift.
