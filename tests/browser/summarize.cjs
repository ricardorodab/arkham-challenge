'use strict';

// Build a short machine-readable acceptance record from the unmodified report.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const reportPath = path.join(root, 'work/browser-results/results.json');
const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
const cases = [], axe = [];
function visit(suites) {
  for (const suite of suites) {
    for (const spec of suite.specs || []) {
      for (const entry of spec.tests || []) {
        const last = entry.results.at(-1);
        cases.push({ name: spec.title, status: last?.status, duration_ms: last?.duration });
        for (const attachment of last?.attachments || []) {
          if (!attachment.name.startsWith('axe-')) continue;
          const data = JSON.parse(attachment.body
            ? Buffer.from(attachment.body, 'base64').toString('utf8')
            : fs.readFileSync(attachment.path, 'utf8'));
          axe.push({
            name: attachment.name, version: data.testEngine.version,
            width: data.testEnvironment.windowWidth,
            height: data.testEnvironment.windowHeight,
            user_agent: data.testEnvironment.userAgent,
            violation_count: data.violations.length,
            incomplete_count: data.incomplete.length,
          });
        }
      }
    }
    visit(suite.suites || []);
  }
}
visit(report.suites);
function screenshots(directory) {
  if (!fs.existsSync(directory)) return [];
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) return screenshots(filename);
    if (!entry.name.endsWith('.png')) return [];
    return [{ path: path.relative(root, filename), sha256: crypto.createHash('sha256')
      .update(fs.readFileSync(filename)).digest('hex') }];
  });
}
const evidence = {
  passed: report.stats.unexpected === 0 && report.errors.length === 0 &&
    cases.length === 18 && cases.every(entry => entry.status === 'passed') &&
    axe.length === 10 && axe.every(scan => scan.violation_count === 0),
  scope: 'Synthetic browser fixture only; no operator credentials or project data.',
  metadata: report.config.metadata,
  node_version: process.version,
  playwright_version: require('@playwright/test/package.json').version,
  axe_version: require('axe-core/package.json').version,
  stats: report.stats,
  cases, accessibility: axe,
  automated_accessibility_note: 'Axe scans supplement keyboard and visual checks; incomplete rules require review and this is not a conformance certification.',
  screenshots: screenshots(path.join(root, 'work/browser-results/artifacts')),
};
const output = path.join(root, 'work/browser-results/evidence.json');
fs.writeFileSync(output, JSON.stringify(evidence, null, 2) + '\n');
console.log(JSON.stringify({ passed: evidence.passed, cases: cases.length,
  scans: axe.length, violations: axe.reduce((n, scan) => n + scan.violation_count, 0),
  screenshots: evidence.screenshots.length, output }, null, 2));
if (!evidence.passed) process.exitCode = 1;
