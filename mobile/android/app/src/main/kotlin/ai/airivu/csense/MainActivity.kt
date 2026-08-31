package ai.airivu.csense

import ai.airivu.csense.ui.ViewModelFactory
import ai.airivu.csense.ui.account.AccountScreen
import ai.airivu.csense.ui.cameras.CameraListScreen
import ai.airivu.csense.ui.dashboard.DashboardScreen
import ai.airivu.csense.ui.incidents.IncidentDetailScreen
import ai.airivu.csense.ui.incidents.IncidentListScreen
import ai.airivu.csense.ui.login.LoginScreen
import ai.airivu.csense.ui.navigation.Screen
import ai.airivu.csense.ui.theme.CSenseTheme
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AccountCircle
import androidx.compose.material.icons.filled.Dashboard
import androidx.compose.material.icons.filled.Videocam
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController

private data class BottomTab(val screen: Screen, val label: String, val icon: androidx.compose.ui.graphics.vector.ImageVector)

private val BOTTOM_TABS = listOf(
    BottomTab(Screen.Dashboard, "Dashboard", Icons.Filled.Dashboard),
    BottomTab(Screen.Incidents, "Incidents", Icons.Filled.Warning),
    BottomTab(Screen.Cameras, "Cameras", Icons.Filled.Videocam),
    BottomTab(Screen.Account, "Account", Icons.Filled.AccountCircle),
)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        val container = (application as CSenseApplication).container
        val factory = ViewModelFactory(container)

        setContent {
            CSenseTheme {
                CSenseApp(factory = factory, startLoggedIn = container.authRepository.isLoggedIn)
            }
        }
    }
}

@Composable
private fun CSenseApp(factory: ViewModelFactory, startLoggedIn: Boolean) {
    val navController = rememberNavController()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = backStackEntry?.destination?.route
    val showBottomBar = BOTTOM_TABS.any { it.screen.route == currentRoute }

    Scaffold(
        bottomBar = {
            if (showBottomBar) {
                CSenseBottomBar(navController, currentRoute)
            }
        },
    ) { padding ->
        NavHost(
            navController = navController,
            startDestination = if (startLoggedIn) Screen.Dashboard.route else Screen.Login.route,
            modifier = Modifier.padding(padding),
        ) {
            composable(Screen.Login.route) {
                LoginScreen(
                    factory = factory,
                    onLoginSuccess = {
                        navController.navigate(Screen.Dashboard.route) {
                            popUpTo(Screen.Login.route) { inclusive = true }
                        }
                    },
                )
            }
            composable(Screen.Dashboard.route) { DashboardScreen(factory) }
            composable(Screen.Incidents.route) {
                IncidentListScreen(
                    factory = factory,
                    onIncidentClick = { id -> navController.navigate(Screen.IncidentDetail.route(id)) },
                )
            }
            composable(Screen.IncidentDetail.route) { entry ->
                val incidentId = entry.arguments?.getString(Screen.IncidentDetail.ARG_INCIDENT_ID) ?: return@composable
                IncidentDetailScreen(factory = factory, incidentId = incidentId, onBack = { navController.popBackStack() })
            }
            composable(Screen.Cameras.route) { CameraListScreen(factory) }
            composable(Screen.Account.route) {
                AccountScreen(
                    factory = factory,
                    onLoggedOut = {
                        navController.navigate(Screen.Login.route) {
                            popUpTo(0) { inclusive = true }
                        }
                    },
                )
            }
        }
    }
}

@Composable
private fun CSenseBottomBar(navController: NavHostController, currentRoute: String?) {
    NavigationBar {
        BOTTOM_TABS.forEach { tab ->
            NavigationBarItem(
                selected = currentRoute == tab.screen.route,
                onClick = {
                    navController.navigate(tab.screen.route) {
                        popUpTo(navController.graph.findStartDestination().id) { saveState = true }
                        launchSingleTop = true
                        restoreState = true
                    }
                },
                icon = { Icon(tab.icon, contentDescription = tab.label) },
                label = { Text(tab.label) },
            )
        }
    }
}
