package ai.airivu.csense.data.network

import ai.airivu.csense.data.network.dto.CameraOut
import ai.airivu.csense.data.network.dto.DashboardOut
import ai.airivu.csense.data.network.dto.IncidentDetail
import ai.airivu.csense.data.network.dto.IncidentPage
import ai.airivu.csense.data.network.dto.IncidentTransitionResult
import ai.airivu.csense.data.network.dto.LicenseOut
import ai.airivu.csense.data.network.dto.ResolveRequest
import ai.airivu.csense.data.network.dto.TransitionRequest
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query

/**
 * Mirrors the real Tenant API's own read/action surface for this app's scope
 * (docs/08_API_GUIDE.md) - dashboard, incidents, cameras, license. Built on the
 * authenticated client (Authorization header + auto-refresh-on-401, see ApiClient.kt).
 */
interface TenantApiService {
    @GET("/api/v1/tenant/dashboard")
    suspend fun getDashboard(): DashboardOut

    @GET("/api/v1/tenant/incidents")
    suspend fun listIncidents(
        @Query("status") status: String? = "active",
        @Query("severity") severity: String? = null,
        @Query("camera_id") cameraId: String? = null,
        @Query("limit") limit: Int = 25,
        @Query("cursor") cursor: String? = null,
    ): IncidentPage

    @GET("/api/v1/tenant/incidents/{incidentId}")
    suspend fun getIncident(@Path("incidentId") incidentId: String): IncidentDetail

    @POST("/api/v1/tenant/incidents/{incidentId}/acknowledge")
    suspend fun acknowledgeIncident(
        @Path("incidentId") incidentId: String,
        @Body body: TransitionRequest = TransitionRequest(),
    ): IncidentTransitionResult

    @POST("/api/v1/tenant/incidents/{incidentId}/investigate")
    suspend fun investigateIncident(
        @Path("incidentId") incidentId: String,
        @Body body: TransitionRequest = TransitionRequest(),
    ): IncidentTransitionResult

    @POST("/api/v1/tenant/incidents/{incidentId}/resolve")
    suspend fun resolveIncident(
        @Path("incidentId") incidentId: String,
        @Body body: ResolveRequest,
    ): IncidentTransitionResult

    @POST("/api/v1/tenant/incidents/{incidentId}/dismiss")
    suspend fun dismissIncident(
        @Path("incidentId") incidentId: String,
        @Body body: ResolveRequest,
    ): IncidentTransitionResult

    @GET("/api/v1/tenant/cameras")
    suspend fun listCameras(): List<CameraOut>

    @GET("/api/v1/tenant/license")
    suspend fun getLicense(): LicenseOut?

    @DELETE("/api/v1/auth/sessions/{sessionId}")
    suspend fun logout(@Path("sessionId") sessionId: String)
}
