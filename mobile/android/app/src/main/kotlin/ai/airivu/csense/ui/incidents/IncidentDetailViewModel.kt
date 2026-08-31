package ai.airivu.csense.ui.incidents

import ai.airivu.csense.data.network.ApiException
import ai.airivu.csense.data.network.dto.IncidentDetail
import ai.airivu.csense.data.repository.IncidentRepository
import ai.airivu.csense.ui.common.UiState
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class IncidentDetailUiState(
    val detail: UiState<IncidentDetail> = UiState.Loading,
    val isActionInFlight: Boolean = false,
    val actionErrorMessage: String? = null,
)

class IncidentDetailViewModel(private val repository: IncidentRepository) : ViewModel() {
    private val _uiState = MutableStateFlow(IncidentDetailUiState())
    val uiState: StateFlow<IncidentDetailUiState> = _uiState.asStateFlow()

    private var incidentId: String? = null

    fun load(incidentId: String) {
        this.incidentId = incidentId
        _uiState.update { it.copy(detail = UiState.Loading) }
        viewModelScope.launch {
            repository.getIncident(incidentId)
                .onSuccess { detail -> _uiState.update { it.copy(detail = UiState.Success(detail)) } }
                .onFailure { error ->
                    _uiState.update { it.copy(detail = UiState.Error(errorMessage(error))) }
                }
        }
    }

    fun acknowledge() = runAction { repository.acknowledge(it) }
    fun investigate() = runAction { repository.investigate(it) }
    fun resolve(resolutionCode: String, reason: String?) = runAction { repository.resolve(it, resolutionCode, reason) }
    fun dismiss(resolutionCode: String, reason: String?) = runAction { repository.dismiss(it, resolutionCode, reason) }

    private fun runAction(call: suspend (String) -> Result<*>) {
        val id = incidentId ?: return
        _uiState.update { it.copy(isActionInFlight = true, actionErrorMessage = null) }
        viewModelScope.launch {
            call(id)
                .onSuccess {
                    _uiState.update { it.copy(isActionInFlight = false) }
                    load(id) // re-fetch: the real, server-computed status/events after
                    // the transition, not a locally-guessed one - load() has its own
                    // independent Loading state for `detail`, unrelated to this flag.
                }
                .onFailure { error ->
                    _uiState.update { it.copy(isActionInFlight = false, actionErrorMessage = errorMessage(error)) }
                }
        }
    }

    private fun errorMessage(error: Throwable): String = when (error) {
        // A 403 here is real and meaningful, not a bug: this app never hides an action
        // button based on a client-side permission guess (deny-wins is enforced
        // server-side, docs/08_API_GUIDE.md) - a member without incident.close simply
        // sees the real refusal if they try to resolve/dismiss.
        is ApiException.Api -> error.message ?: "That action could not be completed."
        is ApiException.Network -> "Could not reach the server. Check your connection."
        else -> "Something went wrong."
    }
}
