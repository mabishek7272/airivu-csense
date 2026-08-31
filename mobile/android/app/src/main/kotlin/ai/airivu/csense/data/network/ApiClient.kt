package ai.airivu.csense.data.network

import ai.airivu.csense.BuildConfig
import ai.airivu.csense.data.auth.PersistentCookieJar
import ai.airivu.csense.data.auth.TokenStore
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.util.concurrent.TimeUnit

/**
 * Builds the two real API surfaces this app talks to (see AuthApiService.kt's own
 * docstring for why login/refresh live on a separate, un-authenticated client than
 * everything else). One `PersistentCookieJar` and one `TokenStore` are shared across
 * both - there is exactly one real session, not two independently-tracked ones.
 */
class ApiClient(
    private val tokenStore: TokenStore,
    private val cookieJar: PersistentCookieJar,
) {
    private val jsonMediaType = JSON_MEDIA_TYPE.toMediaType()

    private val loggingInterceptor = HttpLoggingInterceptor().apply {
        // BODY only in a debug build - the access token and refresh cookie must never
        // reach logcat in a release build (TRD-SEC-003's own "no secret in a log"
        // discipline, applied here the same way the backend applies it server-side).
        level = if (BuildConfig.DEBUG) HttpLoggingInterceptor.Level.BASIC else HttpLoggingInterceptor.Level.NONE
    }

    /** No Authorization header, no auto-refresh Authenticator - see AuthApiService.kt. */
    private val bareClient: OkHttpClient = OkHttpClient.Builder()
        .cookieJar(cookieJar)
        .addInterceptor(HostHeaderInterceptor())
        .addInterceptor(loggingInterceptor)
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    val authApiService: AuthApiService = Retrofit.Builder()
        .baseUrl(BuildConfig.API_BASE_URL)
        .client(bareClient)
        .addConverterFactory(ApiJson.asConverterFactory(jsonMediaType))
        .build()
        .create(AuthApiService::class.java)

    private val authenticatedClient: OkHttpClient = bareClient.newBuilder()
        .addInterceptor(AuthInterceptor(tokenStore))
        .authenticator(SessionAuthenticator(tokenStore, authApiService))
        .build()

    val tenantApiService: TenantApiService = Retrofit.Builder()
        .baseUrl(BuildConfig.API_BASE_URL)
        .client(authenticatedClient)
        .addConverterFactory(ApiJson.asConverterFactory(jsonMediaType))
        .build()
        .create(TenantApiService::class.java)

    private companion object {
        const val JSON_MEDIA_TYPE = "application/json"
    }
}
