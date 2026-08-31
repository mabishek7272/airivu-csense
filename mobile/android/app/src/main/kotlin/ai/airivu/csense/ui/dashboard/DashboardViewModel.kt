package ai.airivu.csense.ui.dashboard

import ai.airivu.csense.data.network.ApiException
import ai.airivu.csense.data.network.dto.DashboardOut
import ai.airivu.csense.data.repository.DashboardRepository
import ai.airivu.csense.ui.common.UiState
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class DashboardViewModel(private val repository: DashboardRepository) : ViewModel() {
    private val _uiState = MutableStateFlow<UiState<DashboardOut>>(UiState.Loading)
    val uiState: StateFlow<UiState<DashboardOut>> = _uiState.asStateFlow()

    init {
        load()
    }

    fun load() {
        _uiState.value = UiState.Loading
        viewModelScope.launch {
            repository.getDashboard()
                .onSuccess { _uiState.value = UiState.Success(it) }
                .onFailure { error ->
                    _uiState.value = UiState.Error(
                        message = (error as? ApiException)?.message ?: "Could not load the dashboard.",
                    )
                }
        }
    }
}
