package ai.airivu.csense.data.network

import kotlinx.serialization.json.Json

/** One shared kotlinx.serialization config - used both for the real Retrofit converter
 * and for parsing an error response body (SafeApiCall.kt) into ProblemResponse, so the
 * two never quietly drift (e.g. one lenient, one strict). */
val ApiJson: Json = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
}
