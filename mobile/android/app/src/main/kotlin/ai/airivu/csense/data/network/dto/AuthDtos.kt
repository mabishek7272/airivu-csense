package ai.airivu.csense.data.network.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Mirrors backend/tenant_api/app/api/auth.py's LoginRequest exactly. */
@Serializable
data class LoginRequest(
    val email: String,
    val password: String,
)

/**
 * Mirrors AuthResponse exactly. Deliberately does NOT carry a refresh token field -
 * the real backend never puts one in this body. It sets `csense_session`/
 * `csense_refresh` as httpOnly cookies (path-scoped to /api/v1/auth) instead, the same
 * way the web Customer CRM already relies on the browser's own cookie jar - this app's
 * OkHttp client carries an equivalent persistent CookieJar (see
 * data/network/PersistentCookieJar.kt) so refresh works the same way here.
 */
@Serializable
data class AuthResponse(
    @SerialName("access_token") val accessToken: String,
    @SerialName("token_type") val tokenType: String = "bearer",
    @SerialName("expires_in") val expiresIn: Int,
    @SerialName("tenant_id") val tenantId: String? = null,
)
