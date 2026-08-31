import { errorMessage } from '../src/utils/errorMessage';
import { ApiError, NetworkError } from '../src/api/client';

describe('errorMessage', () => {
  it('surfaces an ApiError message verbatim', () => {
    const error = new ApiError({ code: 'forbidden', httpStatusCode: 403, retryable: false, message: 'Not allowed.' });
    expect(errorMessage(error)).toBe('Not allowed.');
  });

  it('surfaces a NetworkError message verbatim', () => {
    const error = new NetworkError('Could not reach the server. Check your connection.');
    expect(errorMessage(error)).toBe('Could not reach the server. Check your connection.');
  });

  it('falls back to a generic message for anything else', () => {
    expect(errorMessage('a plain string')).toBe('Something went wrong. Please try again.');
    expect(errorMessage(null)).toBe('Something went wrong. Please try again.');
  });
});
