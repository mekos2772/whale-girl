// Metadata-only audit trail (official-style, ~/.dsh-computer-use/audit/).
// Records never contain arguments, app content, screenshots or errors — only
// shapes and sizes — so the log is safe to keep next to the user's files.
import { appendFile, mkdir, rename, rm, stat } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { join } from 'node:path';

let enabled = true;
let writeChain = Promise.resolve();

const MAX_LOG_BYTES = 10 * 1024 * 1024;   // rotate at 10MB, keep one older file

export function setAuditEnabled(value) {
  enabled = value !== false;
}

function stateRoot() {
  return process.env.DSH_COMPUTER_USE_HOME || join(homedir(), '.dsh-computer-use');
}

/** Hash the app identifier: the log shows which app shape was touched, not its name. */
function appHash(app) {
  if (app == null || app === '') return null;
  return `sha256:${createHash('sha256').update(String(app)).digest('hex').slice(0, 16)}`;
}

export function auditToolCall({ method, app, inputBytes, outcome, durationMs, via = null, resultBytes = 0 }) {
  if (!enabled) return;
  const record = {
    timestamp: new Date().toISOString(),
    runId: randomUUID(),
    method,
    app: appHash(app),
    inputBytes,
    outcome,
    durationMs,
    via,
    resultBytes,
  };
  const dir = join(stateRoot(), 'audit');
  const file = join(dir, 'computer-use.jsonl');
  // Serialize rotation + append. Concurrent tool completions must not race two
  // renames or interleave records around the size check.
  writeChain = writeChain.then(async () => {
    await mkdir(dir, { recursive: true });
    // Long-run guard: rotate the JSONL instead of growing without bound.
    try {
      const info = await stat(file);
      if (info.size >= MAX_LOG_BYTES) {
        const rotated = `${file}.1`;
        await rm(rotated, { force: true });
        await rename(file, rotated);
      }
    } catch { /* missing file is fine */ }
    await appendFile(file, `${JSON.stringify(record)}\n`, 'utf8');
  })
    .catch(() => { /* audit is best-effort and must never break tool flow */ });
}

/** Test/teardown seam: wait until all queued audit writes have settled. */
export function flushAudit() {
  return writeChain;
}
