package ai.airivu.csense.data.network

import ai.airivu.csense.data.network.dto.ProblemResponse
import retrofit2.HttpException
import java.io.IOException

/**
 * Every real error this backend returns has one stable shape (docs/08_API_GUIDE.md's
 * own "Error shape" section, csense_shared.errors.ProblemResponse) - this is that same
 * contract, surfaced as a real Kotlin exception type instead of an opaque HttpException
 * a UI layer would have to re-parse itself.
 */
sealed class ApiException(message: String, cause: Throwable? = null) : Exception(message, cause) {
    /** A real, well-formed error response from the API - `code` is the stable,
     * machine-readable field to match on (e.g. "quota_exceeded", "license_restricted"),
     * the same discipline docs/08_API_GUIDE.md tells every integrator to follow. */
    class Api(
        val code: String,
        val httpStatusCode: Int,
        val retryable: Boolean,
        message: String,
    ) : ApiException(message)

    /** The request never reached the server, or no response came back at all - a real
     * connectivity problem, not something the server said. */
    class Network(message: String, cause: Throwable) : ApiException(message, cause)

    /** Something else entirely - a real bug, not a modeled failure mode. */
    class Unexpected(message: String, cause: Throwable) : ApiException(message, cause)
}

/** Wraps a real Retrofit call, translating a failure into the sealed ApiException
 * hierarchy above rather than letting a raw HttpException/IOException reach the UI
 * layer unclassified. */
suspend fun <T> safeApiCall(call: suspend () -> T): Result<T> = try {
    Result.success(call())
} catch (e: HttpException) {
    val rawBody = e.response()?.errorBody()?.string()
    val problem = rawBody?.let { body ->
        runCatching { ApiJson.decodeFromString(ProblemResponse.serializer(), body) }.getOrNull()
    }
    Result.failure(
        ApiException.Api(
            code = problem?.code ?: "http_${e.code()}",
            httpStatusCode = e.code(),
            retryable = problem?.retryable ?: false,
            message = problem?.message ?: e.message(),
        )
    )
} catch (e: IOException) {
    Result.failure(ApiException.Network("Could not reach the server. Check your connection.", e))
} catch (e: Exception) {
    Result.failure(ApiException.Unexpected(e.message ?: "Something went wrong.", e))
}
