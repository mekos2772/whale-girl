// lib/update.js — optional npm update reminder for the plugin itself.
// Non-blocking and fail-silent: runs once per plugin start, hits the registry
// at most once every 24h, and never throws into the plugin lifecycle. Any
// failure (offline, registry error, malformed response) is swallowed.
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname } from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { name: PKG_NAME, version: LOCAL_VERSION } = require('../package.json');

// Honor the user's npm mirror (e.g. npmmirror) when DSH was started from a
// shell that has one configured; fall back to the official registry.
const REGISTRY = String(process.env.npm_config_registry || 'https://registry.npmjs.org').replace(/\/+$/, '');
const ENC_NAME = PKG_NAME.replace('/', '%2F'); // scoped names must be %-encoded in registry URLs
const STATE_FILE = `${homedir()}/.dsh-computer-use/update.json`;
const CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;

export function compareVersions(a, b) {
  const pa = String(a).split('-')[0].split('.').map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split('-')[0].split('.').map((n) => parseInt(n, 10) || 0);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

// opts are test seams; production callers pass only the logger.
export async function checkPluginUpdate(logger, opts = {}) {
  const fetchImpl = opts.fetchImpl || fetch;
  const stateFile = opts.stateFile || STATE_FILE;
  const registry = opts.registry || REGISTRY;
  const now = opts.now ?? Date.now();
  try {
    const [state, res] = await Promise.all([
      readFile(stateFile, 'utf8')
        .then((s) => { try { return JSON.parse(s); } catch { return {}; } })
        .catch(() => ({})),
      fetchImpl(`${registry}/${ENC_NAME}/latest`, {
        signal: AbortSignal.timeout(5000),
        headers: { accept: 'application/vnd.npm.install-v1+json' },
      }),
    ]);
    if (!res.ok) return;
    const latest = String((await res.json())?.version ?? '');
    if (!latest || compareVersions(latest, LOCAL_VERSION) <= 0) return;

    // Remind at most once per release and at most once per 24h: writing the
    // state first means a crashed process cannot double-remind.
    if (state?.seenVersion === latest) return;
    if (state?.lastChecked && now - Number(state.lastChecked) < CHECK_INTERVAL_MS) return;
    await mkdir(dirname(stateFile), { recursive: true });
    await writeFile(stateFile, JSON.stringify({ lastChecked: now, seenVersion: latest }));
    if (typeof logger?.warn === 'function') {
      logger.warn(`npm 更新可用: ${PKG_NAME} ${LOCAL_VERSION} → ${latest} —— 在安装该插件的 profile 目录执行 "npm i ${PKG_NAME}@latest" 即可升级`);
    }
  } catch {
    // offline / registry unreachable / quota errors — never disturb the session
  }
}