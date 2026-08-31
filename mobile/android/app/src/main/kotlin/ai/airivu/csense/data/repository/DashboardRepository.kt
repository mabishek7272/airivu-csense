package ai.airivu.csense.data.repository

import ai.airivu.csense.data.network.TenantApiService
import ai.airivu.csense.data.network.dto.DashboardOut
import ai.airivu.csense.data.network.safeApiCall

class DashboardRepository(private val api: TenantApiService) {
    suspend fun getDashboard(): Result<DashboardOut> = safeApiCall { api.getDashboard() }
}
