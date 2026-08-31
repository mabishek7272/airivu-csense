package ai.airivu.csense.ui.incidents

import ai.airivu.csense.data.network.ApiException
import ai.airivu.csense.data.network.dto.IncidentSummary
import ai.airivu.csense.data.repository.IncidentRepository
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class IncidentListUiState(
    val incidents: List<IncidentSummary> = emptyList(),
    val statusFilter: String? = "active",
    val isLoading: Boolean = true,
    val isLoadingMore: Boolean = false,
    val errorMessage: String? = null,
    val nextCursor: String? = null,
)

class IncidentListViewModel(private val repository: IncidentRepository) : ViewModel() {
    private val _uiState = MutableStateFlow(IncidentListUiState())
    val uiState: StateFlow<IncidentListUiState> = _uiState.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        _uiState.update { it.copy(isLoading = true, errorMessage = null) }
        viewModelScope.launch {
            repository.listIncidents(status = _uiState.value.statusFilter, cursor = null)
                .onSuccess { page ->
                    _uiState.update {
                        it.copy(isLoading = false, incidents = page.items, nextCursor = page.nextCursor)
                    }
                }
                .onFailure { error -> _uiState.update { it.copy(isLoading = false, errorMessage = errorMessage(error)) } }
        }
    }

    fun loadMore() {
        val state = _uiState.value
        val cursor = state.nextCursor ?: return
        if (state.isLoadingMore) return
        _uiState.update { it.copy(isLoadingMore = true) }
        viewModelScope.launch {
            repository.listIncidents(status = state.statusFilter, cursor = cursor)
                .onSuccess { page ->
                    _uiState.update {
                        it.copy(
                            isLoadingMore = false,
                            incidents = it.incidents + page.items,
                            nextCursor = page.nextCursor,
                        )
                    }
                }
                .onFailure { error -> _uiState.update { it.copy(isLoadingMore = false, errorMessage = errorMessage(error)) } }
        }
    }

    fun setStatusFilter(status: String?) {
        _uiState.update { it.copy(statusFilter = status) }
        refresh()
    }

    private fun errorMessage(error: Throwable): String = when (error) {
        is ApiException.Api -> error.message ?: "Could not load incidents."
        is ApiException.Network -> "Could not reach the server. Check your connection."
        else -> "Something went wrong."
    }
}
