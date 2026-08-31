package ai.airivu.csense.ui.account

import ai.airivu.csense.data.network.dto.LicenseOut
import ai.airivu.csense.ui.ViewModelFactory
import ai.airivu.csense.ui.common.FullScreenError
import ai.airivu.csense.ui.common.FullScreenLoading
import ai.airivu.csense.ui.common.UiState
import ai.airivu.csense.ui.common.formatTimestamp
import ai.airivu.csense.ui.theme.SeverityCritical
import ai.airivu.csense.ui.theme.SeverityLow
import ai.airivu.csense.ui.theme.SeverityMedium
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel

@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
fun AccountScreen(
    factory: ViewModelFactory,
    onLoggedOut: () -> Unit,
) {
    val viewModel: AccountViewModel = viewModel(factory = factory)
    val licenseState by viewModel.licenseState.collectAsState()
    val loggedOut by viewModel.loggedOut.collectAsState()

    LaunchedEffect(loggedOut) {
        if (loggedOut) onLoggedOut()
    }

    Scaffold(
        topBar = { TopAppBar(title = { Text("Account") }) },
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding).padding(16.dp)) {
            Text(text = "License", style = MaterialTheme.typography.titleMedium)
            androidx.compose.foundation.layout.Spacer(Modifier.padding(top = 8.dp))

            when (val s = licenseState) {
                is UiState.Loading -> FullScreenLoading(Modifier.fillMaxWidth())
                is UiState.Error -> FullScreenError(s.message, onRetry = viewModel::loadLicense)
                is UiState.Success -> LicenseCard(s.data)
            }

            androidx.compose.foundation.layout.Spacer(Modifier.weight(1f))

            OutlinedButton(onClick = viewModel::logout, modifier = Modifier.fillMaxWidth()) {
                Text("Sign out")
            }
        }
    }
}

@Composable
private fun LicenseCard(license: LicenseOut?) {
    if (license == null) {
        Text(
            text = "No license assigned yet. Contact your reseller or AIRIVU.",
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        return
    }
    val statusColor = when (license.status) {
        "active" -> SeverityLow
        "grace", "scheduled" -> SeverityMedium
        else -> SeverityCritical
    }
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(text = license.planName, style = MaterialTheme.typography.titleMedium)
                Text(text = license.status, color = statusColor, style = MaterialTheme.typography.titleMedium)
            }
            Text(
                text = "Started ${formatTimestamp(license.startsAt)}" +
                    (license.expiresAt?.let { " · Expires ${formatTimestamp(it)}" } ?: " · No expiry"),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp),
            )
            if (license.status == "grace" && license.graceEndsAt != null) {
                Text(
                    text = "Renew by ${formatTimestamp(license.graceEndsAt)} to avoid interruption",
                    color = SeverityMedium,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
            if (license.quotaUsage.isNotEmpty()) {
                androidx.compose.foundation.layout.Spacer(Modifier.padding(top = 8.dp))
                license.quotaUsage.forEach { quota ->
                    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(text = quota.quotaCode, style = MaterialTheme.typography.bodySmall)
                        Text(
                            text = "${quota.consumedValue + quota.reservedValue} / ${quota.limitValue}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
}
