package ai.airivu.csense.data.repository

import ai.airivu.csense.data.network.TenantApiService
import ai.airivu.csense.data.network.dto.IncidentDetail
import ai.airivu.csense.data.network.dto.IncidentPage
import ai.airivu.csense.data.network.dto.IncidentTransitionResult
import ai.airivu.csense.data.network.dto.ResolveRequest
import ai.airivu.csense.data.network.dto.TransitionRequest
import ai.airivu.csense.data.network.safeApiCall

class IncidentRepository(private val api: TenantApiService) {
    suspend fun listIncidents(status: String? = "active", cursor: String? = null): Result<IncidentPage> =
        safeApiCall { api.listIncidents(status = status, cursor = cursor) }

    suspend fun getIncident(incidentId: String): Result<IncidentDetail> =
        safeApiCall { api.getIncident(incidentId) }

    suspend fun acknowledge(incidentId: String, reason: String? = null): Result<IncidentTransitionResult> =
        safeApiCall { api.acknowledgeIncident(incidentId, TransitionRequest(reason)) }

    suspend fun investigate(incidentId: String, reason: String? = null): Result<IncidentTransitionResult> =
        safeApiCall { api.investigateIncident(incidentId, TransitionRequest(reason)) }

    suspend fun resolve(incidentId: String, resolutionCode: String, reason: String? = null): Result<IncidentTransitionResult> =
        safeApiCall { api.resolveIncident(incidentId, ResolveRequest(resolutionCode, reason)) }

    suspend fun dismiss(incidentId: String, resolutionCode: String, reason: String? = null): Result<IncidentTransitionResult> =
        safeApiCall { api.dismissIncident(incidentId, ResolveRequest(resolutionCode, reason)) }
}
