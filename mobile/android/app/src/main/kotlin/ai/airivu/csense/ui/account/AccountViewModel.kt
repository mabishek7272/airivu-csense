package ai.airivu.csense.ui.account

import ai.airivu.csense.data.network.dto.LicenseOut
import ai.airivu.csense.data.repository.AuthRepository
import ai.airivu.csense.data.repository.LicenseRepository
import ai.airivu.csense.ui.common.UiState
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class AccountViewModel(
    private val authRepository: AuthRepository,
    private val licenseRepository: LicenseRepository,
) : ViewModel() {
    private val _licenseState = MutableStateFlow<UiState<LicenseOut?>>(UiState.Loading)
    val licenseState: StateFlow<UiState<LicenseOut?>> = _licenseState.asStateFlow()

    private val _loggedOut = MutableStateFlow(false)
    val loggedOut: StateFlow<Boolean> = _loggedOut.asStateFlow()

    init {
        loadLicense()
    }

    fun loadLicense() {
        _licenseState.value = UiState.Loading
        viewModelScope.launch {
            licenseRepository.getLicense()
                .onSuccess { _licenseState.value = UiState.Success(it) }
                .onFailure { _licenseState.value = UiState.Error("Could not load license information.") }
        }
    }

    fun logout() {
        viewModelScope.launch {
            authRepository.logout() // best-effort server-side revoke; local session is
            // always cleared regardless (AuthRepository.logout()'s own docstring).
            _loggedOut.value = true
        }
    }
}
