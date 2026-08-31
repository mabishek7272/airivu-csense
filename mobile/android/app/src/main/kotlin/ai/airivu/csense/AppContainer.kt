package ai.airivu.csense

import ai.airivu.csense.data.auth.PersistentCookieJar
import ai.airivu.csense.data.auth.TokenStore
import ai.airivu.csense.data.network.ApiClient
import ai.airivu.csense.data.repository.AuthRepository
import ai.airivu.csense.data.repository.CameraRepository
import ai.airivu.csense.data.repository.DashboardRepository
import ai.airivu.csense.data.repository.IncidentRepository
import ai.airivu.csense.data.repository.LicenseRepository
import android.content.Context

/**
 * A plain, manual dependency container - deliberately not Hilt/Dagger. This app's
 * dependency graph is small and entirely single-instance (one token store, one cookie
 * jar, one API client, five repositories), and a manual container is real, readable,
 * and adds no annotation-processing build cost - a genuine tradeoff worth naming, not
 * an oversight (see mobile/android/README.md).
 */
class AppContainer(context: Context) {
    private val appContext = context.applicationContext

    val tokenStore: TokenStore by lazy { TokenStore(appContext) }
    val cookieJar: PersistentCookieJar by lazy { PersistentCookieJar(appContext) }
    private val apiClient: ApiClient by lazy { ApiClient(tokenStore, cookieJar) }

    val authRepository: AuthRepository by lazy {
        AuthRepository(tokenStore, cookieJar, apiClient.authApiService, apiClient.tenantApiService)
    }
    val dashboardRepository: DashboardRepository by lazy { DashboardRepository(apiClient.tenantApiService) }
    val incidentRepository: IncidentRepository by lazy { IncidentRepository(apiClient.tenantApiService) }
    val cameraRepository: CameraRepository by lazy { CameraRepository(apiClient.tenantApiService) }
    val licenseRepository: LicenseRepository by lazy { LicenseRepository(apiClient.tenantApiService) }
}
