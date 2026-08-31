// https://docs.expo.dev/guides/using-eslint/
const { defineConfig } = require('eslint/config');
const expoConfig = require("eslint-config-expo/flat");

module.exports = defineConfig([
  expoConfig,
  {
    ignores: ["dist/*"],
  },
  {
    // Manual Jest mocks (__mocks__/**) reference the `jest` global too, same as
    // __tests__/** already gets from eslint-config-expo's own jest override.
    files: ["__mocks__/**/*.js"],
    languageOptions: {
      globals: { jest: "readonly" },
    },
  },
]);
