package ai.airivu.csense.ui.common

/** One shared shape for "a screen loading real data from the API" - reused by every
 * ViewModel in this app rather than each inventing its own loading/error/data enum. */
sealed interface UiState<out T> {
    data object Loading : UiState<Nothing>
    data class Success<T>(val data: T) : UiState<T>
    data class Error(val message: String, val retryable: Boolean = true) : UiState<Nothing>
}
