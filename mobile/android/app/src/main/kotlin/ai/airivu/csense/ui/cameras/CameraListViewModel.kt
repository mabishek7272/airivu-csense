package ai.airivu.csense.ui.cameras

import ai.airivu.csense.data.network.ApiException
import ai.airivu.csense.data.network.dto.CameraOut
import ai.airivu.csense.data.repository.CameraRepository
import ai.airivu.csense.ui.common.UiState
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class CameraListViewModel(private val repository: CameraRepository) : ViewModel() {
    private val _uiState = MutableStateFlow<UiState<List<CameraOut>>>(UiState.Loading)
    val uiState: StateFlow<UiState<List<CameraOut>>> = _uiState.asStateFlow()

    init {
        load()
    }

    fun load() {
        _uiState.value = UiState.Loading
        viewModelScope.launch {
            repository.listCameras()
                .onSuccess { _uiState.value = UiState.Success(it) }
                .onFailure { error ->
                    val message = when (error) {
                        is ApiException.Api -> error.message ?: "Could not load cameras."
                        is ApiException.Network -> "Could not reach the server. Check your connection."
                        else -> "Something went wrong."
                    }
                    _uiState.value = UiState.Error(message)
                }
        }
    }
}
