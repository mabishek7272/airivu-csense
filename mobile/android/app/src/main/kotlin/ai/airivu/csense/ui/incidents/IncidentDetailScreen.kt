package ai.airivu.csense.ui.incidents

import ai.airivu.csense.data.network.dto.IncidentDetail
import ai.airivu.csense.data.network.dto.IncidentEventOut
import ai.airivu.csense.ui.ViewModelFactory
import ai.airivu.csense.ui.common.FullScreenError
import ai.airivu.csense.ui.common.FullScreenLoading
import ai.airivu.csense.ui.common.IncidentStatusBadge
import ai.airivu.csense.ui.common.SeverityBadge
import ai.airivu.csense.ui.common.UiState
import ai.airivu.csense.ui.common.formatTimestamp
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.IconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel

private val ACTIVE_STATUSES = setOf("open", "acknowledged", "investigating", "escalated")

@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
fun IncidentDetailScreen(
    factory: ViewModelFactory,
    incidentId: String,
    onBack: () -> Unit,
) {
    val viewModel: IncidentDetailViewModel = viewModel(factory = factory)
    val state by viewModel.uiState.collectAsState()
    var resolveDialogMode by remember { mutableStateOf<String?>(null) } // "resolve" | "dismiss" | null

    LaunchedEffect(incidentId) { viewModel.load(incidentId) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Incident") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
    ) { padding ->
        when (val detailState = state.detail) {
            is UiState.Loading -> FullScreenLoading(Modifier.padding(padding))
            is UiState.Error -> FullScreenError(
                detailState.message,
                onRetry = { viewModel.load(incidentId) },
                modifier = Modifier.padding(padding),
            )
            is UiState.Success -> IncidentDetailContent(
                detail = detailState.data,
                isActionInFlight = state.isActionInFlight,
                actionErrorMessage = state.actionErrorMessage,
                onAcknowledge = viewModel::acknowledge,
                onInvestigate = viewModel::investigate,
                onResolveClick = { resolveDialogMode = "resolve" },
                onDismissClick = { resolveDialogMode = "dismiss" },
                modifier = Modifier.padding(padding),
            )
        }
    }

    if (resolveDialogMode != null) {
        ResolutionDialog(
            title = if (resolveDialogMode == "resolve") "Resolve incident" else "Dismiss incident",
            onConfirm = { code, reason ->
                if (resolveDialogMode == "resolve") viewModel.resolve(code, reason) else viewModel.dismiss(code, reason)
                resolveDialogMode = null
            },
            onCancel = { resolveDialogMode = null },
        )
    }
}

@Composable
private fun IncidentDetailContent(
    detail: IncidentDetail,
    isActionInFlight: Boolean,
    actionErrorMessage: String?,
    onAcknowledge: () -> Unit,
    onInvestigate: () -> Unit,
    onResolveClick: () -> Unit,
    onDismissClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
    ) {
        Text(text = "#${detail.incidentNumber}  ${detail.title}", style = MaterialTheme.typography.headlineSmall)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(top = 8.dp)) {
            SeverityBadge(detail.severity)
            IncidentStatusBadge(detail.status)
        }
        detail.summary?.let {
            Text(text = it, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(top = 12.dp))
        }

        Column(modifier = Modifier.padding(top = 16.dp)) {
            DetailRow("Type", detail.typeCode)
            DetailRow("Detections", detail.detectionCount.toString())
            DetailRow("First detected", formatTimestamp(detail.firstDetectedAt))
            DetailRow("Last detected", formatTimestamp(detail.lastDetectedAt))
            detail.acknowledgedAt?.let { DetailRow("Acknowledged", formatTimestamp(it)) }
            detail.resolutionCode?.let { DetailRow("Resolution", it) }
            detail.resolutionSummary?.let { DetailRow("Resolution summary", it) }
        }

        if (actionErrorMessage != null) {
            Text(
                text = actionErrorMessage,
                color = MaterialTheme.colorScheme.error,
                modifier = Modifier.padding(top = 12.dp),
            )
        }

        if (detail.status in ACTIVE_STATUSES) {
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                modifier = Modifier.fillMaxWidth().padding(top = 16.dp),
            ) {
                if (detail.status == "open") {
                    OutlinedButton(onClick = onAcknowledge, enabled = !isActionInFlight) { Text("Acknowledge") }
                }
                if (detail.status == "open" || detail.status == "acknowledged") {
                    OutlinedButton(onClick = onInvestigate, enabled = !isActionInFlight) { Text("Investigate") }
                }
            }
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            ) {
                Button(onClick = onResolveClick, enabled = !isActionInFlight) { Text("Resolve") }
                OutlinedButton(onClick = onDismissClick, enabled = !isActionInFlight) { Text("Dismiss") }
            }
        }

        HorizontalDivider(modifier = Modifier.padding(vertical = 20.dp))
        Text(text = "History", style = MaterialTheme.typography.titleMedium)
        detail.events.forEach { event -> EventRow(event) }
    }
}

@Composable
private fun DetailRow(label: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(text = label, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(text = value)
    }
}

@Composable
private fun EventRow(event: IncidentEventOut) {
    Column(modifier = Modifier.padding(vertical = 8.dp)) {
        Text(text = event.eventType, style = MaterialTheme.typography.bodyMedium)
        Text(
            text = "${formatTimestamp(event.occurredAt)} · ${event.actorType}",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun ResolutionDialog(
    title: String,
    onConfirm: (resolutionCode: String, reason: String?) -> Unit,
    onCancel: () -> Unit,
) {
    var resolutionCode by remember { mutableStateOf("") }
    var reason by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = onCancel,
        title = { Text(title) },
        text = {
            Column {
                OutlinedTextField(
                    value = resolutionCode,
                    onValueChange = { resolutionCode = it },
                    label = { Text("Resolution code") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    value = reason,
                    onValueChange = { reason = it },
                    label = { Text("Reason (optional)") },
                    modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onConfirm(resolutionCode.trim(), reason.trim().ifBlank { null }) },
                enabled = resolutionCode.isNotBlank(),
            ) { Text("Confirm") }
        },
        dismissButton = { TextButton(onClick = onCancel) { Text("Cancel") } },
    )
}
