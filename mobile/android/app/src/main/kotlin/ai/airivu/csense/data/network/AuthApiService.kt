package ai.airivu.csense.data.network

import ai.airivu.csense.data.network.dto.AuthResponse
import ai.airivu.csense.data.network.dto.LoginRequest
import retrofit2.http.Body
import retrofit2.http.POST

/**
 * Mirrors backend/tenant_api/app/api/auth.py's own pre-auth surface. Built on the bare
 * client (cookie jar only - no Authorization header, no auto-refresh Authenticator,
 * see ApiClient.kt) deliberately: `login` has no token yet to send, and `refresh` is
 * exactly what would need to trigger *itself* if it went through the authenticator,
 * which is precisely the infinite loop SessionAuthenticator's own guard exists to avoid
 * by never using this interface's client for anything but these two calls.
 */
interface AuthApiService {
    @POST("/api/v1/auth/login")
    suspend fun login(@Body body: LoginRequest): AuthResponse

    /** No request body - the real refresh token travels as the `csense_refresh` cookie,
     * attached automatically by the shared PersistentCookieJar. */
    @POST("/api/v1/auth/refresh")
    suspend fun refresh(): AuthResponse
}
