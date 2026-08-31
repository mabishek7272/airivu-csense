package ai.airivu.csense.ui.incidents

import ai.airivu.csense.data.network.dto.IncidentSummary
import ai.airivu.csense.ui.ViewModelFactory
import ai.airivu.csense.ui.common.EmptyState
import ai.airivu.csense.ui.common.FullScreenError
import ai.airivu.csense.ui.common.FullScreenLoading
import ai.airivu.csense.ui.common.IncidentStatusBadge
import ai.airivu.csense.ui.common.SeverityBadge
import ai.airivu.csense.ui.common.formatTimestamp
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
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

private val STATUS_FILTERS = listOf(
    "active" to "Active", null to "All", "resolved" to "Resolved", "dismissed" to "Dismissed",
)

@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
fun IncidentListScreen(
    factory: ViewModelFactory,
    onIncidentClick: (String) -> Unit,
) {
    val viewModel: IncidentListViewModel = viewModel(factory = factory)
    val state by viewModel.uiState.collectAsState()
    val listState = androidx.compose.foundation.lazy.rememberLazyListState()

    // Load the next page a couple of items before the real end of the list - a common,
    // real infinite-scroll pattern, wired to the same cursor the backend's own keyset
    // pagination returns (incidents.py's IncidentPage.next_cursor).
    LaunchedEffect(listState, state.incidents.size) {
        snapshotFlowLastVisible(listState).collect { lastVisible ->
            if (lastVisible != null && lastVisible >= state.incidents.size - 3 && state.nextCursor != null) {
                viewModel.loadMore()
            }
        }
    }

    Scaffold(
        topBar = { TopAppBar(title = { Text("Incidents") }) },
    ) { padding ->
        Column(modifier = Modifier.padding(padding)) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState())
                    .padding(horizontal = 16.dp, vertical = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                STATUS_FILTERS.forEach { (value, label) ->
                    FilterChip(
                        selected = state.statusFilter == value,
                        onClick = { viewModel.setStatusFilter(value) },
                        label = { Text(label) },
                    )
                }
            }

            when {
                state.isLoading -> FullScreenLoading()
                state.errorMessage != null && state.incidents.isEmpty() ->
                    FullScreenError(state.errorMessage!!, onRetry = viewModel::refresh)
                state.incidents.isEmpty() -> EmptyState("No incidents here.")
                else -> LazyColumn(
                    state = listState,
                    contentPadding = PaddingValues(16.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    items(state.incidents, key = { it.id }) { incident ->
                        IncidentRow(incident, onClick = { onIncidentClick(incident.id) })
                    }
                    if (state.isLoadingMore) {
                        item {
                            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                                CircularProgressIndicator(modifier = Modifier.padding(16.dp))
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun IncidentRow(incident: IncidentSummary, onClick: () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        onClick = onClick,
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(
                    text = "#${incident.incidentNumber}  ${incident.title}",
                    style = MaterialTheme.typography.titleMedium,
                    modifier = Modifier.weight(1f, fill = true),
                )
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(top = 6.dp)) {
                SeverityBadge(incident.severity)
                IncidentStatusBadge(incident.status)
            }
            Text(
                text = "Last detected ${formatTimestamp(incident.lastDetectedAt)}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 6.dp),
            )
        }
    }
}

/** A small helper so the "near the end of the list" check above reads cleanly - the
 * last fully-visible item index from Compose's own LazyListState. */
private fun snapshotFlowLastVisible(listState: androidx.compose.foundation.lazy.LazyListState) =
    androidx.compose.runtime.snapshotFlow {
        listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index
    }
