package ai.airivu.csense.data.auth

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Holds the short-lived access token (and the tenant it was issued for), backed by
 * Android's Keystore-wrapped EncryptedSharedPreferences - the access token is a bearer
 * credential good for `expires_in` seconds against this tenant's data, and belongs in
 * the same class of "never leaves the device unencrypted" storage the backend's own
 * `csense_shared.security.secret_store` gives camera credentials, not a plain
 * SharedPreferences file.
 *
 * The refresh token itself is never stored here - it never reaches this app as a value
 * to store at all (see AuthDtos.kt's own note); it lives only in the OkHttp cookie jar's
 * own encrypted storage (PersistentCookieJar.kt).
 */
class TokenStore(context: Context) {
    private val prefs: SharedPreferences by lazy {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "csense_token_store",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    var accessToken: String?
        get() = prefs.getString(KEY_ACCESS_TOKEN, null)
        set(value) = prefs.edit().putString(KEY_ACCESS_TOKEN, value).apply()

    /** Wall-clock expiry, computed once at issuance (now + expires_in) - a local
     * estimate used only to decide whether to proactively refresh before a request,
     * never trusted as the actual authority (the server's own 401 + the
     * SessionAuthenticator's reactive refresh is what's actually correct if this
     * estimate and the server ever disagree, e.g. after the device clock drifts). */
    var accessTokenExpiresAtEpochSeconds: Long
        get() = prefs.getLong(KEY_EXPIRES_AT, 0L)
        set(value) = prefs.edit().putLong(KEY_EXPIRES_AT, value).apply()

    var tenantId: String?
        get() = prefs.getString(KEY_TENANT_ID, null)
        set(value) = prefs.edit().putString(KEY_TENANT_ID, value).apply()

    val isLoggedIn: Boolean
        get() = !accessToken.isNullOrBlank()

    fun clear() {
        prefs.edit().clear().apply()
    }

    private companion object {
        const val KEY_ACCESS_TOKEN = "access_token"
        const val KEY_EXPIRES_AT = "access_token_expires_at"
        const val KEY_TENANT_ID = "tenant_id"
    }
}
