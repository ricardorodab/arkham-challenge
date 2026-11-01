'use strict';

const { spawn } = require('node:child_process');
const { ROOT, PYTHON, environment } = require('./environment.cjs');

const server = spawn(PYTHON, ['-m', 'tests.browser.fixture_server'], {
  cwd: ROOT, env: environment(), stdio: 'inherit', windowsHide: true,
});
let stopping = false;
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    if (!stopping) {
      stopping = true;
      server.kill('SIGTERM');
    }
  });
}
process.on('exit', () => { if (server.exitCode === null) server.kill('SIGTERM'); });
server.on('error', error => { console.error(error.message); process.exitCode = 1; });
server.on('exit', code => { process.exitCode = stopping ? 0 : (code ?? 1); });
