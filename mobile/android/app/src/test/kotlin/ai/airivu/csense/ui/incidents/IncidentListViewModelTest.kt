package ai.airivu.csense.ui.incidents

import ai.airivu.csense.data.network.dto.IncidentPage
import ai.airivu.csense.data.network.dto.IncidentSummary
import ai.airivu.csense.data.repository.IncidentRepository
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
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class IncidentListViewModelTest {
    private val dispatcher = StandardTestDispatcher()
    private val repository: IncidentRepository = mockk()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun summary(id: String) = IncidentSummary(
        id = id, incidentNumber = 1, typeCode = "person.restricted_zone", severity = "high", status = "open",
        title = "Test incident", summary = null, cameraId = "cam-1", siteId = "site-1", detectionCount = 1,
        firstDetectedAt = "2026-08-31T00:00:00Z", lastDetectedAt = "2026-08-31T00:00:00Z", acknowledgedAt = null,
    )

    @Test
    fun `loadMore appends to the existing list using the real next_cursor, not replacing it`() = runTest(dispatcher) {
        coEvery { repository.listIncidents(status = "active", cursor = null) } returns
            Result.success(IncidentPage(items = listOf(summary("a"), summary("b")), nextCursor = "cursor-1"))
        coEvery { repository.listIncidents(status = "active", cursor = "cursor-1") } returns
            Result.success(IncidentPage(items = listOf(summary("c")), nextCursor = null))

        val viewModel = IncidentListViewModel(repository)
        dispatcher.scheduler.advanceUntilIdle()
        assertEquals(listOf("a", "b"), viewModel.uiState.value.incidents.map { it.id })
        assertEquals("cursor-1", viewModel.uiState.value.nextCursor)

        viewModel.loadMore()
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(listOf("a", "b", "c"), viewModel.uiState.value.incidents.map { it.id })
        assertNull(viewModel.uiState.value.nextCursor)
    }

    @Test
    fun `loadMore does nothing once there is no next_cursor - no wasted request`() = runTest(dispatcher) {
        coEvery { repository.listIncidents(status = "active", cursor = null) } returns
            Result.success(IncidentPage(items = listOf(summary("a")), nextCursor = null))

        val viewModel = IncidentListViewModel(repository)
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.loadMore()
        dispatcher.scheduler.advanceUntilIdle()

        coVerify(exactly = 1) { repository.listIncidents(status = "active", cursor = null) }
        coVerify(exactly = 0) { repository.listIncidents(status = "active", cursor = "cursor-1") }
    }

    @Test
    fun `changing the status filter triggers a fresh, non-appending reload`() = runTest(dispatcher) {
        coEvery { repository.listIncidents(status = "active", cursor = null) } returns
            Result.success(IncidentPage(items = listOf(summary("a")), nextCursor = null))
        coEvery { repository.listIncidents(status = "resolved", cursor = null) } returns
            Result.success(IncidentPage(items = listOf(summary("z")), nextCursor = null))

        val viewModel = IncidentListViewModel(repository)
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.setStatusFilter("resolved")
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(listOf("z"), viewModel.uiState.value.incidents.map { it.id })
        assertEquals("resolved", viewModel.uiState.value.statusFilter)
    }
}
