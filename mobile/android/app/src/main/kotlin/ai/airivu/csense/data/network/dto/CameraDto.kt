package ai.airivu.csense.data.network.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/** Mirrors backend/tenant_api/app/api/cameras.py's CameraOut exactly. Never carries a
 * password - `hasCredentials` is the write-only discipline every camera-credential
 * screen in this codebase already follows (see cameras.py's own docstring). */
@Serializable
data class CameraOut(
    val id: String,
    @SerialName("site_id") val siteId: String,
    @SerialName("site_name") val siteName: String? = null,
    @SerialName("zone_id") val zoneId: String? = null,
    val name: String,
    val code: String,
    val vendor: String? = null,
    val model: String? = null,
    val hostname: String? = null,
    @SerialName("rtsp_port") val rtspPort: Int? = null,
    @SerialName("main_stream_path") val mainStreamPath: String? = null,
    @SerialName("sub_stream_path") val subStreamPath: String? = null,
    val username: String? = null,
    @SerialName("rtsp_transport") val rtspTransport: String,
    val status: String,
    @SerialName("has_credentials") val hasCredentials: Boolean,
    @SerialName("stream_profile") val streamProfile: JsonElement? = null,
    @SerialName("last_probed_at") val lastProbedAt: String? = null,
    @SerialName("last_frame_at") val lastFrameAt: String? = null,
    @SerialName("last_error") val lastError: String? = null,
    @SerialName("created_at") val createdAt: String,
)
