package ai.airivu.csense.ui.common

import ai.airivu.csense.ui.theme.SeverityCritical
import ai.airivu.csense.ui.theme.SeverityHigh
import ai.airivu.csense.ui.theme.SeverityLow
import ai.airivu.csense.ui.theme.SeverityMedium
import ai.airivu.csense.ui.theme.StatusAcknowledged
import ai.airivu.csense.ui.theme.StatusDismissed
import ai.airivu.csense.ui.theme.StatusEscalated
import ai.airivu.csense.ui.theme.StatusInvestigating
import ai.airivu.csense.ui.theme.StatusOpen
import ai.airivu.csense.ui.theme.StatusResolved
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

@Composable
fun FullScreenLoading(modifier: Modifier = Modifier) {
    Column(
        modifier = modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        CircularProgressIndicator()
    }
}

@Composable
fun FullScreenError(
    message: String,
    onRetry: (() -> Unit)? = null,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier.fillMaxSize().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(text = message, textAlign = androidx.compose.ui.text.style.TextAlign.Center)
        if (onRetry != null) {
            androidx.compose.foundation.layout.Spacer(Modifier.padding(top = 12.dp))
            Button(onClick = onRetry) { Text("Retry") }
        }
    }
}

@Composable
fun EmptyState(message: String, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier.fillMaxSize().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(text = message, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/** Mirrors the customer-crm frontend's own severity badge convention
 * (critical/high/medium/low), so the colors an incident shows here match what the same
 * incident shows on the web. */
@Composable
fun SeverityBadge(severity: String, modifier: Modifier = Modifier) {
    val color = when (severity.lowercase()) {
        "critical" -> SeverityCritical
        "high" -> SeverityHigh
        "medium" -> SeverityMedium
        "low" -> SeverityLow
        else -> Color.Gray
    }
    Badge(text = severity.replaceFirstChar { it.uppercase() }, color = color, modifier = modifier)
}

/** Mirrors incidents.py's own real status set (open/acknowledged/investigating/
 * escalated/resolved/dismissed). */
@Composable
fun IncidentStatusBadge(status: String, modifier: Modifier = Modifier) {
    val color = when (status.lowercase()) {
        "open" -> StatusOpen
        "acknowledged" -> StatusAcknowledged
        "investigating" -> StatusInvestigating
        "escalated" -> StatusEscalated
        "resolved" -> StatusResolved
        "dismissed" -> StatusDismissed
        else -> Color.Gray
    }
    Badge(text = status.replaceFirstChar { it.uppercase() }, color = color, modifier = modifier)
}

@Composable
private fun Badge(text: String, color: Color, modifier: Modifier = Modifier) {
    Surface(
        color = color.copy(alpha = 0.15f),
        contentColor = color,
        shape = RoundedCornerShape(6.dp),
        modifier = modifier,
    ) {
        Text(
            text = text,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp),
            fontSize = 12.sp,
            fontWeight = FontWeight.Medium,
        )
    }
}
