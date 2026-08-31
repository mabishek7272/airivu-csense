package ai.airivu.csense.ui.common

import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle

// java.time is available unconditionally from API 26 - no desugaring dependency needed
// since this app's own minSdk (app/build.gradle.kts) is already 26.
private val displayFormatter = DateTimeFormatter.ofLocalizedDateTime(FormatStyle.MEDIUM, FormatStyle.SHORT)
    .withZone(ZoneId.systemDefault())

/** Parses a real backend ISO-8601 timestamp (every `dt.datetime` field this API
 * returns) into a locale-aware, human-readable string. Returns the raw value unparsed
 * rather than crashing if the server ever sends something unexpected - a malformed
 * timestamp should degrade a label, not the whole screen. */
fun formatTimestamp(iso8601: String?): String {
    if (iso8601.isNullOrBlank()) return "—"
    return runCatching { displayFormatter.format(Instant.parse(iso8601)) }.getOrDefault(iso8601)
}
