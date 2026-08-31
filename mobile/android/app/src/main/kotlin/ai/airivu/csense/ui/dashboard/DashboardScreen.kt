package ai.airivu.csense.ui.dashboard

import ai.airivu.csense.data.network.dto.DashboardOut
import ai.airivu.csense.ui.ViewModelFactory
import ai.airivu.csense.ui.common.FullScreenError
import ai.airivu.csense.ui.common.FullScreenLoading
import ai.airivu.csense.ui.common.UiState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel

private data class DashboardTile(val label: String, val value: Int)

@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
fun DashboardScreen(factory: ViewModelFactory) {
    val viewModel: DashboardViewModel = viewModel(factory = factory)
    val state by viewModel.uiState.collectAsState()

    Scaffold(
        topBar = { TopAppBar(title = { Text("Dashboard") }) },
    ) { padding ->
        when (val s = state) {
            is UiState.Loading -> FullScreenLoading(Modifier.padding(padding))
            is UiState.Error -> FullScreenError(s.message, onRetry = viewModel::load, modifier = Modifier.padding(padding))
            is UiState.Success -> DashboardContent(s.data, padding)
        }
    }
}

@Composable
private fun DashboardContent(data: DashboardOut, padding: PaddingValues) {
    val tiles = listOf(
        DashboardTile("Open incidents", data.incidentsOpenCount),
        DashboardTile("Total incidents", data.incidentsTotalCount),
        DashboardTile("Cameras", data.camerasCount),
        DashboardTile("Sites", data.sitesCount),
        DashboardTile("Team members", data.teamMembersCount),
    )
    LazyVerticalGrid(
        columns = GridCells.Fixed(2),
        contentPadding = PaddingValues(16.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
        modifier = Modifier.fillMaxSize().padding(padding),
    ) {
        items(tiles) { tile -> DashboardTileCard(tile) }
    }
}

@Composable
private fun DashboardTileCard(tile: DashboardTile) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(text = tile.value.toString(), style = MaterialTheme.typography.headlineMedium)
            Text(text = tile.label, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}
