package ai.airivu.csense.data.network

import ai.airivu.csense.BuildConfig
import ai.airivu.csense.data.auth.TokenStore
import okhttp3.Interceptor
import okhttp3.Response

/**
 * Sets the `Host` header Traefik's own path/host-based routing needs
 * (infra/traefik/dynamic.yml) - the same header every e2e script in the backend repo
 * passes explicitly when it isn't talking to a real DNS-resolved domain. Against a real
 * production `https://` domain (docs/10_PRODUCTION_DEPLOYMENT_GUIDE.md), this is a
 * harmless no-op (the header already matches the URL's own host); it only does real work
 * against the local dev stack's `10.0.2.2` alias, which carries no meaningful Host of
 * its own.
 */
class HostHeaderInterceptor : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request().newBuilder()
            .header("Host", BuildConfig.API_HOST_HEADER)
            .build()
        return chain.proceed(request)
    }
}

/**
 * Attaches the bearer access token to every request through this client. Deliberately
 * simple - no refresh-on-expiry logic lives here (that is SessionAuthenticator's job,
 * reacting to a real 401 rather than a locally-estimated expiry, which is what the
 * server actually enforces).
 */
class AuthInterceptor(private val tokenStore: TokenStore) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val token = tokenStore.accessToken
        val request = if (token.isNullOrBlank()) {
            chain.request()
        } else {
            chain.request().newBuilder()
                .header("Authorization", "Bearer $token")
                .build()
        }
        return chain.proceed(request)
    }
}
