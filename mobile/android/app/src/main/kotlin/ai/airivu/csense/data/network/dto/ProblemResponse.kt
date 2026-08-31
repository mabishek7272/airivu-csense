package ai.airivu.csense.data.network.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/**
 * Mirrors csense_shared.errors.ProblemResponse exactly - every error this backend
 * returns has this shape (docs/08_API_GUIDE.md's own "Error shape" section). `code` is
 * the stable, machine-readable field to match on; `message` is for display.
 */
@Serializable
data class ProblemResponse(
    val code: String,
    val message: String,
    val details: JsonElement? = null,
    @SerialName("correlation_id") val correlationId: String? = null,
    val retryable: Boolean = false,
)
