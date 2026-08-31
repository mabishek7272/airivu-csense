package ai.airivu.csense.ui.cameras

import ai.airivu.csense.data.network.dto.CameraOut
import ai.airivu.csense.ui.ViewModelFactory
import ai.airivu.csense.ui.common.EmptyState
import ai.airivu.csense.ui.common.FullScreenError
import ai.airivu.csense.ui.common.FullScreenLoading
import ai.airivu.csense.ui.common.UiState
import ai.airivu.csense.ui.common.formatTimestamp
import ai.airivu.csense.ui.theme.SeverityCritical
import ai.airivu.csense.ui.theme.SeverityLow
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel

@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
fun CameraListScreen(factory: ViewModelFactory) {
    val viewModel: CameraListViewModel = viewModel(factory = factory)
    val state by viewModel.uiState.collectAsState()

    Scaffold(
        topBar = { TopAppBar(title = { Text("Cameras") }) },
    ) { padding ->
        when (val s = state) {
            is UiState.Loading -> FullScreenLoading(Modifier.padding(padding))
            is UiState.Error -> FullScreenError(s.message, onRetry = viewModel::load, modifier = Modifier.padding(padding))
            is UiState.Success -> if (s.data.isEmpty()) {
                EmptyState("No cameras yet.", Modifier.padding(padding))
            } else {
                LazyColumn(
                    contentPadding = PaddingValues(16.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.padding(padding),
                ) {
                    items(s.data, key = { it.id }) { camera -> CameraRow(camera) }
                }
            }
        }
    }
}

@Composable
private fun CameraRow(camera: CameraOut) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier.padding(16.dp).fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            val statusColor = if (camera.status == "ready" || camera.status == "online") SeverityLow else SeverityCritical
            androidx.compose.foundation.layout.Box(
                modifier = Modifier
                    .size(10.dp)
                    .clip(CircleShape)
                    .background(statusColor),
            )
            Column(modifier = Modifier.weight(1f)) {
                Text(text = camera.name, style = MaterialTheme.typography.titleMedium)
                camera.siteName?.let {
                    Text(text = it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Text(
                    text = "Status: ${camera.status}" +
                        (camera.lastProbedAt?.let { " · Probed ${formatTimestamp(it)}" } ?: ""),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                camera.lastError?.let {
                    Text(text = it, style = MaterialTheme.typography.bodySmall, color = SeverityCritical)
                }
            }
        }
    }
}
