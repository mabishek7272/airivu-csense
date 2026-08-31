package ai.airivu.csense.data.network

import ai.airivu.csense.data.auth.TokenStore
import kotlinx.coroutines.runBlocking
import okhttp3.Authenticator
import okhttp3.Request
import okhttp3.Response
import okhttp3.Route

/**
 * Reacts to a real 401 from the Tenant API by attempting a real refresh (POST
 * /api/v1/auth/refresh, using the shared cookie jar's own `csense_refresh` cookie) and
 * retrying the original request once with the new access token - the same "the server's
 * own 401 is authoritative, not a locally-guessed expiry" reasoning TokenStore's own
 * `accessTokenExpiresAtEpochSeconds` field is deliberately NOT used to drive this.
 *
 * Two loop guards, both real failure modes this would otherwise hit:
 *  - `responseCount(response) >= 2` - never retry more than once (a second 401 after a
 *    successful-looking refresh means something is genuinely wrong, not transiently
 *    expired - keep failing rather than retrying forever).
 *  - `authApiService` runs on its own bare client (Interceptors.kt / ApiClient.kt) with
 *    no Authenticator of its own - a 401 from `/auth/refresh` itself can never
 *    recursively trigger this same authenticator.
 */
class SessionAuthenticator(
    private val tokenStore: TokenStore,
    private val authApiService: AuthApiService,
) : Authenticator {
    override fun authenticate(route: Route?, response: Response): Request? {
        if (responseCount(response) >= 2) return null

        val refreshed = runBlocking {
            runCatching { authApiService.refresh() }.getOrNull()
        }
        if (refreshed == null) {
            // The refresh session itself is gone (expired, revoked, or already used) -
            // no amount of retrying fixes this; force a real logout state so the UI
            // layer can route back to the login screen rather than looping on 401s.
            tokenStore.clear()
            return null
        }

        tokenStore.accessToken = refreshed.accessToken
        tokenStore.accessTokenExpiresAtEpochSeconds =
            System.currentTimeMillis() / 1000 + refreshed.expiresIn
        refreshed.tenantId?.let { tokenStore.tenantId = it }

        return response.request.newBuilder()
            .header("Authorization", "Bearer ${refreshed.accessToken}")
            .build()
    }

    private fun responseCount(response: Response): Int {
        var count = 1
        var prior = response.priorResponse
        while (prior != null) {
            count++
            prior = prior.priorResponse
        }
        return count
    }
}
