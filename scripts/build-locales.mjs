/** Build deterministic browser and GNU gettext catalogs from shared UTF-8 interface messages. */
import { readFile, readdir, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const frontend = path.join(root, 'src/outage_explorer/frontend');
const directory = path.join(frontend, 'locales/es');
const messages = {};
for (const filename of (await readdir(directory)).filter((name) => name.endsWith('.json')).sort()) {
  const entries = JSON.parse(await readFile(path.join(directory, filename), 'utf8'));
  for (const [source, translation] of Object.entries(entries)) {
    if (!source || typeof translation !== 'string' || !translation.trim())
      throw new Error(`Invalid translation in ${filename}: ${source}`);
    const placeholders = (value) =>
      [...value.matchAll(/\{([A-Za-z][A-Za-z0-9_]*)\}/g)].map((match) => match[1]).sort();
    if (JSON.stringify(placeholders(source)) !== JSON.stringify(placeholders(translation)))
      throw new Error(`Translation placeholders differ in ${filename}: ${source}`);
    if (Object.hasOwn(messages, source) && messages[source] !== translation)
      throw new Error(`Conflicting translation in ${filename}: ${source}`);
    messages[source] = translation;
  }
}

const ordered = Object.fromEntries(
  Object.entries(messages).sort(([left], [right]) =>
    Buffer.compare(Buffer.from(left), Buffer.from(right)),
  ),
);
const browser = Buffer.from(
  `/* Generated interface catalog. Rebuild with npm run build:i18n. */\nexport default ${JSON.stringify(ordered, null, 2)};\n`,
);
const metadata =
  'Content-Type: text/plain; charset=UTF-8\nLanguage: es\nPlural-Forms: nplurals=2; plural=(n != 1);\n';
const pairs = [['', metadata], ...Object.entries(ordered)].sort(([left], [right]) =>
  Buffer.compare(Buffer.from(left), Buffer.from(right)),
);
const count = pairs.length;
const originalTable = 28;
const translatedTable = originalTable + count * 8;
let position = translatedTable + count * 8;
const originals = pairs.map(([source]) => Buffer.from(source));
const translations = pairs.map(([, translation]) => Buffer.from(translation));
const header = Buffer.alloc(position);
header.writeUInt32LE(0x950412de, 0);
header.writeUInt32LE(count, 8);
header.writeUInt32LE(originalTable, 12);
header.writeUInt32LE(translatedTable, 16);
for (const [table, values] of [
  [originalTable, originals],
  [translatedTable, translations],
]) {
  values.forEach((value, index) => {
    header.writeUInt32LE(value.length, table + index * 8);
    header.writeUInt32LE(position, table + index * 8 + 4);
    position += value.length + 1;
  });
}
const compiled = Buffer.concat([
  header,
  ...[...originals, ...translations].flatMap((value) => [value, Buffer.from([0])]),
]);
const outputs = [
  [path.join(frontend, 'static/outage_explorer/messages.es.js'), browser],
  [path.join(frontend, 'locale/es/LC_MESSAGES/django.mo'), compiled],
];
for (const [filename, bytes] of outputs) {
  if (process.argv.includes('--check')) {
    if (!bytes.equals(await readFile(filename)))
      throw new Error(`${path.relative(root, filename)} is stale. Run npm run build:i18n.`);
  } else {
    await mkdir(path.dirname(filename), { recursive: true });
    await writeFile(filename, bytes);
  }
}
console.log(
  `${process.argv.includes('--check') ? 'Verified' : 'Built'} ${count - 1} Spanish interface messages for browser and Django.`,
);
