package ai.airivu.csense.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable

private val LightColors = lightColorScheme(
    primary = CSenseNavy,
    secondary = CSenseGreenDark,
    tertiary = CSenseGreen,
)

private val DarkColors = darkColorScheme(
    primary = CSenseGreen,
    secondary = CSenseNavyLight,
    tertiary = CSenseGreenDark,
)

@Composable
fun CSenseTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val colorScheme = if (darkTheme) DarkColors else LightColors
    MaterialTheme(
        colorScheme = colorScheme,
        typography = MaterialTheme.typography,
        content = content,
    )
}
