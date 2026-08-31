package ai.airivu.csense.data.network.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Mirrors backend/tenant_api/app/api/dashboard.py's DashboardOut exactly. */
@Serializable
data class DashboardOut(
    @SerialName("sites_count") val sitesCount: Int,
    @SerialName("cameras_count") val camerasCount: Int,
    @SerialName("incidents_open_count") val incidentsOpenCount: Int,
    @SerialName("incidents_total_count") val incidentsTotalCount: Int,
    @SerialName("team_members_count") val teamMembersCount: Int,
)
