package ai.airivu.csense.ui.login

import ai.airivu.csense.data.network.ApiException
import ai.airivu.csense.data.repository.AuthRepository
import app.cash.turbine.test
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.mockk
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class LoginViewModelTest {
    private val dispatcher = StandardTestDispatcher()
    private val authRepository: AuthRepository = mockk()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `blank email or password is refused before ever calling the repository`() = runTest(dispatcher) {
        val viewModel = LoginViewModel(authRepository)
        viewModel.onEmailChange("")
        viewModel.onPasswordChange("")

        viewModel.login()

        assertEquals("Enter your email and password.", viewModel.uiState.value.errorMessage)
        coVerify(exactly = 0) { authRepository.login(any(), any()) }
    }

    @Test
    fun `a successful login flips loginSucceeded and clears the error`() = runTest(dispatcher) {
        coEvery { authRepository.login("owner@example.com", "a-real-password") } returns Result.success(Unit)
        val viewModel = LoginViewModel(authRepository)
        viewModel.onEmailChange("owner@example.com")
        viewModel.onPasswordChange("a-real-password")

        viewModel.uiState.test {
            skipItems(1) // the initial (email/password-only) emission from the two onX calls above
            viewModel.login()

            val loading = awaitItem()
            assertTrue(loading.isLoading)

            dispatcher.scheduler.advanceUntilIdle()
            val done = awaitItem()
            assertFalse(done.isLoading)
            assertTrue(done.loginSucceeded)
            assertNull(done.errorMessage)
        }
    }

    @Test
    fun `a real backend refusal surfaces its own message, not a generic one`() = runTest(dispatcher) {
        coEvery { authRepository.login(any(), any()) } returns Result.failure(
            ApiException.Api(code = "authentication_required", httpStatusCode = 401, retryable = false, message = "Invalid email or password."),
        )
        val viewModel = LoginViewModel(authRepository)
        viewModel.onEmailChange("owner@example.com")
        viewModel.onPasswordChange("wrong-password")

        viewModel.login()
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals("Invalid email or password.", viewModel.uiState.value.errorMessage)
        assertFalse(viewModel.uiState.value.loginSucceeded)
    }

    @Test
    fun `a network failure gets a real, distinct message`() = runTest(dispatcher) {
        coEvery { authRepository.login(any(), any()) } returns Result.failure(
            ApiException.Network("unreachable", RuntimeException("boom")),
        )
        val viewModel = LoginViewModel(authRepository)
        viewModel.onEmailChange("owner@example.com")
        viewModel.onPasswordChange("a-real-password")

        viewModel.login()
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals("Could not reach the server. Check your connection.", viewModel.uiState.value.errorMessage)
    }
}
