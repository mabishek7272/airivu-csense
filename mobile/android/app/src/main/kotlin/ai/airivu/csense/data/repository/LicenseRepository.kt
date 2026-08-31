package ai.airivu.csense.data.repository

import ai.airivu.csense.data.network.TenantApiService
import ai.airivu.csense.data.network.dto.LicenseOut
import ai.airivu.csense.data.network.safeApiCall

class LicenseRepository(private val api: TenantApiService) {
    /** Null is a valid, common answer (no license assigned yet) - the same "not an
     * error" contract the real endpoint documents (license.py's own docstring). */
    suspend fun getLicense(): Result<LicenseOut?> = safeApiCall { api.getLicense() }
}
