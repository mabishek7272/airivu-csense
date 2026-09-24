const { AndroidConfig } = require('@expo/config-plugins');

module.exports = function withBlockRecordAudio(config) {
  config = AndroidConfig.Permissions.withBlockedPermissions(config, ['android.permission.RECORD_AUDIO']);
  return config;
};
