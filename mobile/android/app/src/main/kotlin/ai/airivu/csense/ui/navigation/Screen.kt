package ai.airivu.csense.ui.navigation

sealed class Screen(val route: String) {
    data object Login : Screen("login")
    data object Dashboard : Screen("dashboard")
    data object Incidents : Screen("incidents")
    data object IncidentDetail : Screen("incidents/{incidentId}") {
        fun route(incidentId: String) = "incidents/$incidentId"
        const val ARG_INCIDENT_ID = "incidentId"
    }
    data object Cameras : Screen("cameras")
    data object Account : Screen("account")
}
