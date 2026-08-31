// This package's real implementation is a native TurboModule (no native binary exists
// under Jest, which runs on plain Node) - see src/auth/cookies.ts's own docstring for
// why this app depends on it at all (reading the httpOnly `csense_session` cookie's
// current value for the logout endpoint's session-id path parameter). Tests that need
// specific cookie behavior mock `../src/auth/cookies` directly instead of this module;
// this stub just lets anything that *imports* cookies.ts (even without exercising it)
// load under Jest without a real device/simulator.
module.exports = {
  get: jest.fn().mockResolvedValue({}),
  clearAll: jest.fn().mockResolvedValue(true),
};
