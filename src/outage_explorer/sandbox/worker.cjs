'use strict';

/**
 * Trusted host bootstrap and bounded transport for one DuckDB-WASM execution.
 *
 * The Python caller authorizes the logical dataset, compiles SQL, verifies its
 * Parquet bytes and supplies Node permission/resource controls. This shim does
 * not authenticate callers or compile a SQL allowlist. SQL/data are never
 * evaluated as JavaScript source.
 *
 * Trusted JavaScript reads the installed npm engine and one packaged, hashed
 * Parquet extension. Those bootstrap reads are distinct from engine authority:
 * submitted SQL sees BUFFER-only WASM imports, never the host NODE_RUNTIME.
 * stdout carries one bounded JSON frame; raw engine diagnostics are suppressed.
 */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const MAX_INPUT = 64 * 1024 * 1024;
const MAX_OUTPUT = 2 * 1024 * 1024;
const EXTENSION_SHA256 = '4845705bbd69fc9ad52878d96a505c73cae4a6c509822079cc2413e5eb437f95';
const EXTENSION_URL = 'https://extensions.duckdb.org/v1.5.4/wasm_eh/parquet.duckdb_extension.wasm';

// Engine diagnostics may contain submitted text or physical implementation
// details. Only this shim's bounded structured response reaches the supervisor.
console.log = console.warn = console.error = () => {};
// Remove general network entry points before loading the trusted engine package.
// The temporary extension bridge below does not perform an HTTP request.
globalThis.fetch = () => { throw new Error('Network unavailable'); };
globalThis.WebSocket = undefined;
const NativeMemory = WebAssembly.Memory;
// Constrain every engine memory construction independently of DuckDB's memory
// setting and the parent OS process limit. Shared linear memory is not accepted.
WebAssembly.Memory = class extends NativeMemory {
  /** @param {WebAssembly.MemoryDescriptor} descriptor Engine memory request. */
  constructor(descriptor) {
    // A true linear-memory ceiling, independently of DuckDB's buffer manager.
    const maximum = Math.min(descriptor.maximum ?? 4096, 4096);
    if (descriptor.initial > maximum || descriptor.shared) throw new Error('sandbox_unavailable');
    super({ ...descriptor, maximum });
  }
};

/**
 * Emit the sole JSON protocol response, replacing an oversized frame with a code.
 * @param {object} value Typed result or static error frame; no raw diagnostics.
 * @returns {void}
 */
function reply(value) {
  const encoded = Buffer.from(JSON.stringify(value));
  if (encoded.length > MAX_OUTPUT) {
    process.stdout.write('{"error":"result_too_large"}');
  } else {
    process.stdout.write(encoded);
  }
}

/**
 * Decode Arrow decimal words as a signed two's-complement integer plus scale.
 * BigInt reconstruction and string placement preserve all stored decimal digits;
 * no intermediate Number conversion or additional rounding is introduced.
 * @param {ArrayLike<number>} value Least-significant 32-bit word first.
 * @param {number} scale Declared fractional digit count.
 * @returns {string} Fixed-point text, including sign and declared trailing zeros.
 */
function decimal(value, scale) {
  let integer = 0n;
  for (let i = value.length - 1; i >= 0; i--) {
    integer = (integer << 32n) | BigInt(value[i] >>> 0);
  }
  if (value[value.length - 1] & 0x80000000) {
    integer -= 1n << BigInt(value.length * 32);
  }
  const negative = integer < 0;
  let digits = (negative ? -integer : integer).toString();
  if (scale > 0) {
    digits = digits.padStart(scale + 1, '0');
    digits = digits.slice(0, -scale) + '.' + digits.slice(-scale);
  }
  return (negative ? '-' : '') + digits;
}

/**
 * Map Arrow scalar metadata to the SQL type labels returned by the API.
 * Unsupported metadata can retain its descriptive type string here; cell()
 * independently rejects values outside the scalar result contract.
 * @param {object} type Arrow field type supplied by the trusted runtime.
 * @returns {string} Logical SQL type label, including decimal precision/scale.
 */
function columnType(type) {
  if (type.typeId === 2) {
    const integer = { 8: 'TINYINT', 16: 'SMALLINT', 32: 'INTEGER', 64: 'BIGINT' }[type.bitWidth];
    return (type.isSigned ? '' : 'U') + integer;
  }
  if (type.typeId === 3) return type.precision === 1 ? 'FLOAT' : 'DOUBLE';
  if (type.typeId === 7) return `DECIMAL(${type.precision},${type.scale})`;
  if (type.typeId === 8) return 'DATE';
  if (type.typeId === 10) return 'TIMESTAMP';
  if (type.typeId === 5) return 'VARCHAR';
  if (type.typeId === 6) return 'BOOLEAN';
  return type.toString();
}

/**
 * Encode one Arrow scalar for JSON without discarding decimal/integer precision.
 * Null/undefined become null; decimals and BigInt become text; date/timestamp
 * values become ISO text. Finite primitive numbers, booleans and strings remain
 * scalars. Nested/list/binary values and non-finite numbers fail closed.
 * @param {*} value Arrow vector cell.
 * @param {object} type Arrow field type controlling exact decimal/date encoding.
 * @returns {null|string|number|boolean} API scalar value.
 * @throws {Error} A static non-finite/unsupported-result code.
 */
function cell(value, type) {
  if (value === null || value === undefined) return null;
  if (type.typeId === 7) return decimal(value, type.scale);
  if (type.typeId === 8) return new Date(Number(value)).toISOString().slice(0, 10);
  if (type.typeId === 10) return new Date(Number(value)).toISOString();
  if (typeof value === 'bigint') return value.toString();
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('non_finite_result');
    return value;
  }
  if (typeof value === 'boolean' || typeof value === 'string') return value;
  // Nested/list/binary outputs are outside the API result contract.
  throw new Error('unsupported_result_type');
}

/**
 * Bootstrap the trusted runtime, lock its capabilities, and execute one query.
 *
 * The npm package supplies the engine JS/WASM; the separately packaged Parquet
 * extension is checked here against its own pinned hash. A bootstrap-only bridge
 * supplies that local extension before SQL execution. It grants no general HTTP
 * or filesystem capability to the engine and closes immediately after LOAD.
 *
 * Only the supplied buffer is registered. Trusted setup creates the logical view,
 * disables extension installation/external access/spill, and locks configuration.
 * Stream Arrow batches into bounded typed rows; the supervisor supplies the hard
 * wall/process limits. Output LIMIT bounds retained rows, not aggregate/sort cost.
 * @param {object} header Validated logical dataset, SQL and output bounds.
 * @param {Buffer} parquet One authorized, already-checksummed input file as bytes.
 * @returns {Promise<object>} Columns, retained rows and runtime identifiers.
 * @throws {Error} Static runtime/type/size failures; main sanitizes other errors.
 */
async function execute(header, parquet) {
  const duck = require(path.join(process.argv[2], '@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs'));
  const extension = fs.readFileSync(path.join(__dirname, 'parquet-v1.5.4-wasm_eh.wasm'));
  if (crypto.createHash('sha256').update(extension).digest('hex') !== EXTENSION_SHA256) {
    throw new Error('sandbox_unavailable');
  }
  // The engine's extension loader otherwise reaches Node's home cache or starts
  // a fetching worker. This loader has exactly one capability: supply the pinned
  // local extension asset during bootstrap. It never performs an HTTP request.
  let bootstrap = true;
  globalThis.XMLHttpRequest = class {
    /** Accept only the synchronous GET for the exact pinned extension identifier. */
    open(method, url, asynchronous) {
      if (!bootstrap || method !== 'GET' || url !== EXTENSION_URL || asynchronous !== false) {
        throw new Error('Network unavailable');
      }
    }
    /** Supply a copy of local extension bytes while bootstrap remains open. */
    send() {
      if (!bootstrap) throw new Error('Network unavailable');
      this.status = 200;
      this.response = extension.buffer.slice(extension.byteOffset, extension.byteOffset + extension.byteLength);
    }
  };
  // These are WASM host imports. Denying them limits the SQL engine even though
  // the trusted JavaScript bootstrap itself can read explicitly granted assets.
  const denied = (module) => duck.failWith(module, 'Host capability unavailable');
  const runtime = { ...duck.DEFAULT_RUNTIME, _udfFunctions: new Map() };
  for (const name of ['openFile', 'syncFile', 'truncateFile', 'readFile', 'writeFile',
    'removeDirectory', 'createDirectory', 'moveFile', 'removeFile', 'callScalarUDF']) {
    runtime[name] = denied;
  }
  // Registration resolves only in-memory buffers; directory probes/globs do not
  // expose a second route to Node's filesystem implementation.
  runtime.getDefaultDataProtocol = () => duck.DuckDBDataProtocol.BUFFER;
  runtime.glob = () => {};
  runtime.checkFile = runtime.checkDirectory = runtime.listDirectoryEntries = () => false;
  const wasmPath = path.join(process.argv[2], '@duckdb/duckdb-wasm/dist/duckdb-eh.wasm');
  const database = await duck.createDuckDB({
    mvp: { mainModule: wasmPath }, eh: { mainModule: wasmPath },
  }, new duck.VoidLogger(), runtime);
  await database.instantiate();
  database.open({
    maximumThreads: 1, arrowLosslessConversion: true,
    query: { castDecimalToDouble: false, castBigIntToDouble: false, castTimestampToDate: false },
  });
  database.registerFileBuffer('input.parquet', new Uint8Array(parquet));
  parquet = null;
  const connection = database.connect();
  try {
    connection.query('LOAD parquet');
    bootstrap = false;
    connection.query("SET memory_limit='96MB'");
    connection.query('SET threads=1');
    connection.query('SET autoload_known_extensions=false');
    connection.query('SET autoinstall_known_extensions=false');
    connection.query('SET allow_community_extensions=false');
    connection.query("SET temp_directory=''");
    connection.query(`CREATE VIEW "${header.dataset_id}" AS SELECT * FROM read_parquet('input.parquet')`);
    connection.query("SET allowed_paths=['input.parquet']");
    connection.query('SET enable_external_access=false');
    connection.query('SET lock_configuration=true');
    // Every capability/configuration restriction above precedes submitted SQL.
    // Incremental batches cap retained transport data, not all engine plan work.
    const reader = await connection.send(header.sql, true);
    await reader.open();
    const fields = reader.schema.fields;
    const columns = fields.map(field => ({ name: field.name, type: columnType(field.type) }));
    const rows = [];
    let encodedBytes = Buffer.byteLength(JSON.stringify(columns)) + 256;
    outer: for await (const batch of reader) {
      const vectors = fields.map((_, index) => batch.getChildAt(index));
      for (let index = 0; index < batch.numRows; index++) {
        const row = fields.map((field, column) => cell(vectors[column].get(index), field.type));
        encodedBytes += Buffer.byteLength(JSON.stringify(row)) + 1;
        if (encodedBytes > header.max_output_bytes) throw new Error('result_too_large');
        rows.push(row);
        if (rows.length >= header.max_rows) break outer;
      }
    }
    return { columns, rows, engine: `DuckDB-WASM ${database.getVersion()}`, sandbox: 'wasm-buffer-v1' };
  } finally {
    connection.close();
    database.reset();
  }
}

/**
 * Read and validate one newline-delimited JSON header followed by Parquet bytes.
 * The bounded input is parent-process transport, not a filesystem path protocol.
 * Validate dataset spelling, SQL size, row/output bounds and binary length before
 * execution; authorization and SQL grammar remain the Python caller's duties.
 * @returns {Promise<void>} Writes one result frame through reply().
 */
async function main() {
  let chunks = [];
  let length = 0;
  for await (const chunk of process.stdin) {
    length += chunk.length;
    if (length > MAX_INPUT + 32768) throw new Error('invalid_request');
    chunks.push(chunk);
  }
  const payload = Buffer.concat(chunks);
  chunks = null;
  const newline = payload.indexOf(10);
  if (newline < 1 || newline > 32768) throw new Error('invalid_request');
  const header = JSON.parse(payload.subarray(0, newline).toString('utf8'));
  const parquet = payload.subarray(newline + 1);
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(header.dataset_id) ||
      typeof header.sql !== 'string' || Buffer.byteLength(header.sql) > 16384 ||
      !Number.isInteger(header.max_rows) || header.max_rows < 1 || header.max_rows > 1001 ||
      !Number.isInteger(header.max_output_bytes) || header.max_output_bytes < 256 ||
      header.max_output_bytes > MAX_OUTPUT || parquet.length < 8 || parquet.length > MAX_INPUT) {
    throw new Error('invalid_request');
  }
  reply(await execute(header, parquet));
}

// Preserve only known public codes. Runtime diagnostics and submitted SQL never
// leave the child; memory-shaped failures collapse to the resource-limit code.
main().catch(error => {
  const known = new Set(['invalid_request', 'sandbox_unavailable', 'result_too_large',
    'non_finite_result', 'unsupported_result_type', 'resource_limit']);
  const code = /out of memory|memory access out of bounds|could not allocate/i.test(error.message)
    ? 'resource_limit' : (known.has(error.message) ? error.message : 'query_failed');
  reply({ error: code });
  process.exitCode = 1;
});
