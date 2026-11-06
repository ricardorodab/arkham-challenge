'use strict';

const fs = require('node:fs');
const path = require('node:path');
const fixture = require('./fixture.json');

const ROOT = path.resolve(__dirname, '../..');
const runId = process.env.OUTAGE_BROWSER_RUN_ID;
if (!runId || !/^[a-zA-Z0-9_-]{1,64}$/.test(runId)) {
  throw new Error('An isolated OUTAGE_BROWSER_RUN_ID is required.');
}
const PYTHON = process.env.OUTAGE_TEST_PYTHON || path.join(
  ROOT, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
);
if (!fs.existsSync(PYTHON)) {
  throw new Error('Set OUTAGE_TEST_PYTHON to the project Python with locked dependencies.');
}
/**
 * Return an isolated child environment for one validated browser run ID.
 * Strip inherited EIA/seed/Python inputs, then select fixture-only credentials
 * and work paths. Future runs intentionally recreate work/; operator .service
 * and analytical data paths are never reused by this launcher.
 */
function environment() {
  const env = { ...process.env };
  for (const name of ['EIA_API_KEY', 'PYTHONPATH', 'PYTHONHOME',
    'OUTAGE_VIEWER_PASSWORD', 'OUTAGE_ANALYST_PASSWORD', 'OUTAGE_ADMIN_PASSWORD']) {
    delete env[name];
  }
  return {
    ...env,
    DJANGO_SETTINGS_MODULE: 'outage_explorer.web.settings',
    OUTAGE_DJANGO_SECRET_KEY: fixture.secret_key,
    OUTAGE_DJANGO_DEBUG: '0',
    OUTAGE_LOCAL_HTTP: '1',
    OUTAGE_PROJECT_DIR: ROOT,
    OUTAGE_STATE_DIR: path.join(ROOT, 'work/browser-state', runId),
    OUTAGE_DATA_DIR: path.join(ROOT, 'work/browser-data', runId),
    OUTAGE_BROWSER_RUN_ID: runId,
    PYTHONUTF8: '1',
  };
}

module.exports = { ROOT, PYTHON, environment, fixture };
