package ai.airivu.csense.data.repository

import ai.airivu.csense.data.network.TenantApiService
import ai.airivu.csense.data.network.dto.CameraOut
import ai.airivu.csense.data.network.safeApiCall

class CameraRepository(private val api: TenantApiService) {
    suspend fun listCameras(): Result<List<CameraOut>> = safeApiCall { api.listCameras() }
}
