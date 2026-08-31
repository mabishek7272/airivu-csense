package ai.airivu.csense.data.repository

import ai.airivu.csense.data.auth.PersistentCookieJar
import ai.airivu.csense.data.auth.TokenStore
import ai.airivu.csense.data.network.AuthApiService
import ai.airivu.csense.data.network.TenantApiService
import ai.airivu.csense.data.network.dto.LoginRequest
import ai.airivu.csense.data.network.safeApiCall

/** The session cookie's own name - see backend/tenant_api/app/api/auth.py's
 * `SESSION_COOKIE_NAME` constant. Needed here (not just inside PersistentCookieJar)
 * because logout() has to look this specific cookie up by name to build the real
 * DELETE /api/v1/auth/sessions/{sessionId} call. */
private const val SESSION_COOKIE_NAME = "csense_session"

class AuthRepository(
    private val tokenStore: TokenStore,
    private val cookieJar: PersistentCookieJar,
    private val authApiService: AuthApiService,
    private val tenantApiService: TenantApiService,
) {
    val isLoggedIn: Boolean get() = tokenStore.isLoggedIn

    suspend fun login(email: String, password: String): Result<Unit> =
        safeApiCall { authApiService.login(LoginRequest(email, password)) }
            .map { auth ->
                tokenStore.accessToken = auth.accessToken
                tokenStore.accessTokenExpiresAtEpochSeconds =
                    System.currentTimeMillis() / 1000 + auth.expiresIn
                tokenStore.tenantId = auth.tenantId
            }

    /** Best-effort real server-side revocation, but the *local* session is always
     * cleared regardless of whether that call succeeds - a user tapping "log out" while
     * offline must still actually be logged out on this device; the server-side session
     * will simply expire on its own if the revoke call never lands. */
    suspend fun logout(): Result<Unit> {
        val sessionId = cookieJar.findCookieValue(SESSION_COOKIE_NAME)
        val result = if (sessionId != null) {
            safeApiCall { tenantApiService.logout(sessionId) }
        } else {
            Result.success(Unit)
        }
        tokenStore.clear()
        cookieJar.clear()
        return result
    }
}
