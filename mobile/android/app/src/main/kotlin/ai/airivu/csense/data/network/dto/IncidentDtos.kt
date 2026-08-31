package ai.airivu.csense.data.network.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/** Mirrors backend/tenant_api/app/api/incidents.py's IncidentSummary exactly.
 * Timestamps stay as raw ISO-8601 strings (the wire format Pydantic's own
 * `datetime` serializes to) - parsed with java.time only where a screen actually
 * needs to format one, rather than pulling a datetime serializer into the data layer.
 */
@Serializable
data class IncidentSummary(
    val id: String,
    @SerialName("incident_number") val incidentNumber: Int,
    @SerialName("type_code") val typeCode: String,
    val severity: String,
    val status: String,
    val title: String,
    val summary: String? = null,
    @SerialName("camera_id") val cameraId: String,
    @SerialName("site_id") val siteId: String,
    @SerialName("detection_count") val detectionCount: Int,
    @SerialName("first_detected_at") val firstDetectedAt: String,
    @SerialName("last_detected_at") val lastDetectedAt: String,
    @SerialName("acknowledged_at") val acknowledgedAt: String? = null,
)

@Serializable
data class IncidentPage(
    val items: List<IncidentSummary>,
    @SerialName("next_cursor") val nextCursor: String? = null,
)

@Serializable
data class IncidentEventOut(
    @SerialName("event_type") val eventType: String,
    @SerialName("actor_type") val actorType: String,
    @SerialName("actor_id") val actorId: String? = null,
    @SerialName("previous_status") val previousStatus: String? = null,
    @SerialName("new_status") val newStatus: String? = null,
    @SerialName("occurred_at") val occurredAt: String,
    val payload: JsonElement? = null,
)

/** Mirrors IncidentDetail(IncidentSummary) - flattened rather than modeled as a
 * subclass, since the wire shape is already flat (Pydantic inheritance just merges
 * fields) and a flat data class is the simpler, more direct match for
 * kotlinx.serialization. */
@Serializable
data class IncidentDetail(
    val id: String,
    @SerialName("incident_number") val incidentNumber: Int,
    @SerialName("type_code") val typeCode: String,
    val severity: String,
    val status: String,
    val title: String,
    val summary: String? = null,
    @SerialName("camera_id") val cameraId: String,
    @SerialName("site_id") val siteId: String,
    @SerialName("detection_count") val detectionCount: Int,
    @SerialName("first_detected_at") val firstDetectedAt: String,
    @SerialName("last_detected_at") val lastDetectedAt: String,
    @SerialName("acknowledged_at") val acknowledgedAt: String? = null,
    @SerialName("resolution_code") val resolutionCode: String? = null,
    @SerialName("resolution_summary") val resolutionSummary: String? = null,
    val metadata: JsonElement? = null,
    val events: List<IncidentEventOut> = emptyList(),
    @SerialName("detection_ids") val detectionIds: List<String> = emptyList(),
)

/** Body for POST .../acknowledge and .../investigate - mirrors TransitionRequest. */
@Serializable
data class TransitionRequest(
    val reason: String? = null,
)

/** Body for POST .../resolve and .../dismiss - mirrors ResolveRequest. */
@Serializable
data class ResolveRequest(
    @SerialName("resolution_code") val resolutionCode: String,
    val reason: String? = null,
)

/** The ad-hoc `{"id", "previous_status", "status"}` dict every transition endpoint
 * returns (incidents.py's own `_transition` helper - not a named Pydantic model on the
 * backend, but a stable, real shape worth naming here). */
@Serializable
data class IncidentTransitionResult(
    val id: String,
    @SerialName("previous_status") val previousStatus: String? = null,
    val status: String,
)
