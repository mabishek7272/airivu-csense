package ai.airivu.csense.data.auth

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl

/**
 * A real, persistent CookieJar - the mobile-app equivalent of a browser's own cookie
 * store, which is what the backend's refresh flow actually assumes exists
 * (`csense_session`/`csense_refresh`, httpOnly, path-scoped to `/api/v1/auth` - see
 * AuthDtos.kt). Without this, "log in once and stay logged in" would be impossible: the
 * refresh token would vanish the moment OkHttp's own in-memory default CookieJar was
 * garbage-collected or the process restarted.
 *
 * Encrypted at rest for the same reason TokenStore is - this is exactly as sensitive as
 * the access token itself (`csense_refresh` is a real, replayable-once credential).
 */
class PersistentCookieJar(context: Context) : CookieJar {
    private val prefs by lazy {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "csense_cookie_store",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    // In-memory mirror, keyed by "name|domain|path" so a re-set cookie replaces its own
    // prior value rather than accumulating duplicates - loaded once from encrypted
    // storage, re-persisted as one JSON blob on every write (there are only ever two
    // real cookies in play here, so this is not a performance concern).
    private val cookies: MutableMap<String, Cookie> by lazy { loadPersisted().toMutableMap() }

    @Synchronized
    override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
        for (cookie in cookies) {
            val key = keyFor(cookie)
            if (cookie.expiresAt <= System.currentTimeMillis()) {
                this.cookies.remove(key) // an expired/deleting Set-Cookie (e.g. logout's own delete_cookie)
            } else {
                this.cookies[key] = cookie
            }
        }
        persist()
    }

    @Synchronized
    override fun loadForRequest(url: HttpUrl): List<Cookie> {
        val now = System.currentTimeMillis()
        val expired = cookies.values.filter { it.expiresAt <= now }
        expired.forEach { cookies.remove(keyFor(it)) }
        if (expired.isNotEmpty()) persist()
        // Cookie.matches() is OkHttp's own real domain/path/secure matching logic -
        // reused rather than reimplemented.
        return cookies.values.filter { it.matches(url) }
    }

    @Synchronized
    fun clear() {
        cookies.clear()
        prefs.edit().clear().apply()
    }

    /** Reads a stored cookie's current value by name (e.g. `csense_session`) - the real
     * logout endpoint needs the session id in its own URL path, and this cookie jar is
     * the only place this app ever sees it (it's never returned in a JSON body, see
     * AuthDtos.kt). Returns null once the cookie has expired or was never set. */
    @Synchronized
    fun findCookieValue(name: String): String? =
        cookies.values.firstOrNull { it.name == name && it.expiresAt > System.currentTimeMillis() }?.value

    private fun keyFor(cookie: Cookie): String = "${cookie.name}|${cookie.domain}|${cookie.path}"

    private fun persist() {
        val persisted = cookies.values.map {
            PersistedCookie(
                name = it.name, value = it.value, domain = it.domain, path = it.path,
                expiresAt = it.expiresAt, secure = it.secure, hostOnly = it.hostOnly,
            )
        }
        prefs.edit().putString(KEY_COOKIES, Json.encodeToString(persisted)).apply()
    }

    private fun loadPersisted(): Map<String, Cookie> {
        val raw = prefs.getString(KEY_COOKIES, null) ?: return emptyMap()
        val persisted = runCatching { Json.decodeFromString<List<PersistedCookie>>(raw) }.getOrElse { emptyList() }
        return persisted.associate { p ->
            val cookie = Cookie.Builder()
                .name(p.name).value(p.value).path(p.path).expiresAt(p.expiresAt)
                .apply {
                    if (p.hostOnly) hostOnlyDomain(p.domain) else domain(p.domain)
                    if (p.secure) secure()
                }
                .build()
            "${p.name}|${p.domain}|${p.path}" to cookie
        }
    }

    @Serializable
    private data class PersistedCookie(
        val name: String,
        val value: String,
        val domain: String,
        val path: String,
        val expiresAt: Long,
        val secure: Boolean,
        val hostOnly: Boolean,
    )

    private companion object {
        const val KEY_COOKIES = "cookies_json"
    }
}
