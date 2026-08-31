import { ApiError, NetworkError } from '../api/client';

/** Every hook in src/hooks/ funnels a caught error through this, so every screen shows
 * the same real message for the same failure rather than each re-deriving its own. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof NetworkError) return error.message;
  if (error instanceof Error) return error.message;
  return 'Something went wrong. Please try again.';
}
