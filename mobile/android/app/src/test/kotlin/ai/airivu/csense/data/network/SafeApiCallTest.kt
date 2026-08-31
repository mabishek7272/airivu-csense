package ai.airivu.csense.data.network

import kotlinx.coroutines.test.runTest
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import retrofit2.HttpException
import retrofit2.Response
import java.io.IOException

/** Real coverage of the one place every screen's error message actually comes from -
 * mirrors the real backend contract (csense_shared.errors.ProblemResponse,
 * docs/08_API_GUIDE.md's own "Error shape" section) with a real JSON body, not a
 * hand-waved exception message. */
class SafeApiCallTest {
    @Test
    fun `a real ProblemResponse error body is parsed into ApiException Api with the real code`() = runTest {
        val body = """
            {"code":"quota_exceeded","message":"This tenant's camera limit (5) is already in use (5).","details":null,"correlation_id":"c-1","retryable":false}
        """.trimIndent()
        val httpException = httpExceptionWithBody(402, body)

        val result = safeApiCall<Unit> { throw httpException }

        assertTrue(result.isFailure)
        val error = result.exceptionOrNull() as ApiException.Api
        assertEquals("quota_exceeded", error.code)
        assertEquals(402, error.httpStatusCode)
        assertEquals("This tenant's camera limit (5) is already in use (5).", error.message)
        assertFalse(error.retryable)
    }

    @Test
    fun `a retryable error preserves that flag`() = runTest {
        val body = """{"code":"rate_limit_exceeded","message":"Too many requests.","retryable":true}"""
        val result = safeApiCall<Unit> { throw httpExceptionWithBody(429, body) }

        val error = result.exceptionOrNull() as ApiException.Api
        assertTrue(error.retryable)
    }

    @Test
    fun `an unparseable error body still produces a usable ApiException, not a crash`() = runTest {
        val result = safeApiCall<Unit> { throw httpExceptionWithBody(500, "not json at all") }

        assertTrue(result.isFailure)
        val error = result.exceptionOrNull() as ApiException.Api
        assertEquals("http_500", error.code)
    }

    @Test
    fun `a real connectivity failure maps to ApiException Network, not Api`() = runTest {
        val result = safeApiCall<Unit> { throw IOException("Unable to resolve host") }

        assertTrue(result.exceptionOrNull() is ApiException.Network)
    }

    @Test
    fun `a real success passes the value through untouched`() = runTest {
        val result = safeApiCall { "real value" }

        assertEquals("real value", result.getOrNull())
    }

    private fun httpExceptionWithBody(code: Int, body: String): HttpException {
        val responseBody = body.toResponseBody("application/json".toMediaType())
        return HttpException(Response.error<Unit>(code, responseBody))
    }
}
