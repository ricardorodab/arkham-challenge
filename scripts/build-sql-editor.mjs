/** Build or verify the self-hosted editor and the licenses of its bundled dependencies. */
import { build } from 'esbuild';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = 'src/outage_explorer/frontend/static/outage_explorer/sql_editor.bundle.js';
const licensesOutput = 'src/outage_explorer/frontend/static/outage_explorer/sql_editor.LICENSE.txt';
const result = await build({
  absWorkingDir: root,
  entryPoints: ['src/outage_explorer/frontend/editor/sql_editor.js'],
  bundle: true,
  format: 'esm',
  target: ['es2022'],
  outfile: output,
  minify: true,
  legalComments: 'inline',
  metafile: true,
  write: false,
  banner: {
    js: '/*! Self-hosted CodeMirror SQL editor. Third-party MIT licenses: sql_editor.LICENSE.txt. Source: frontend/editor/sql_editor.js. */',
  },
});
const packages = new Set(
  Object.keys(result.metafile.inputs).flatMap((input) => {
    const match = input.replaceAll('\\', '/').match(/^node_modules\/((?:@[^/]+\/)?[^/]+)\//);
    return match ? [match[1]] : [];
  }),
);
const licenses = [
  'Bundled SQL editor dependency licenses\n\nThis generated file contains the licenses of packages included in sql_editor.bundle.js.\n',
];
for (const name of [...packages].sort()) {
  const base = path.join(root, 'node_modules', name);
  const metadata = JSON.parse(await readFile(path.join(base, 'package.json'), 'utf8'));
  let license;
  for (const filename of ['LICENSE', 'LICENSE.txt', 'LICENSE.md']) {
    try {
      license = await readFile(path.join(base, filename), 'utf8');
      break;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
  if (!license) throw new Error(`No distributable license found for ${name}.`);
  licenses.push(`\n--- ${name} ${metadata.version} ---\n\n${license.trim()}\n`);
}
const artifacts = [
  [output, result.outputFiles[0].contents],
  [licensesOutput, Buffer.from(licenses.join('').replaceAll('\r\n', '\n'))],
];
for (const [filename, expected] of artifacts) {
  const target = path.join(root, filename);
  if (process.argv.includes('--check')) {
    const existing = await readFile(target);
    if (!Buffer.from(expected).equals(existing))
      throw new Error(`${filename} is stale. Run npm run build:editor.`);
  } else await writeFile(target, expected);
}
console.log(
  `${process.argv.includes('--check') ? 'Verified' : 'Built'} SQL editor: ${result.outputFiles[0].contents.length.toLocaleString('en-US')} bytes; ${packages.size} dependency licenses.`,
);
