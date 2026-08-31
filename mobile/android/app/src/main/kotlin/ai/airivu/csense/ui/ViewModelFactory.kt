package ai.airivu.csense.ui

import ai.airivu.csense.AppContainer
import ai.airivu.csense.ui.account.AccountViewModel
import ai.airivu.csense.ui.cameras.CameraListViewModel
import ai.airivu.csense.ui.dashboard.DashboardViewModel
import ai.airivu.csense.ui.incidents.IncidentDetailViewModel
import ai.airivu.csense.ui.incidents.IncidentListViewModel
import ai.airivu.csense.ui.login.LoginViewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.CreationExtras

/**
 * One factory for every ViewModel in this app, backed by AppContainer - the manual-DI
 * counterpart to what Hilt's own `@HiltViewModel` would generate (see AppContainer.kt's
 * own docstring for why this app doesn't pull Hilt in).
 */
class ViewModelFactory(private val container: AppContainer) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>, extras: CreationExtras): T = when (modelClass) {
        LoginViewModel::class.java -> LoginViewModel(container.authRepository) as T
        DashboardViewModel::class.java -> DashboardViewModel(container.dashboardRepository) as T
        IncidentListViewModel::class.java -> IncidentListViewModel(container.incidentRepository) as T
        IncidentDetailViewModel::class.java -> IncidentDetailViewModel(container.incidentRepository) as T
        CameraListViewModel::class.java -> CameraListViewModel(container.cameraRepository) as T
        AccountViewModel::class.java -> AccountViewModel(container.authRepository, container.licenseRepository) as T
        else -> throw IllegalArgumentException("Unknown ViewModel class: ${modelClass.name}")
    }
}
