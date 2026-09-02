// uia.ps1 invoker. The kernel runs in resident mode (`uia.ps1 --serve`): one
// PowerShell process answers one JSON request per stdin line with one JSON
// response line, so tool calls skip the ~0.5s process + UIA Add-Type startup
// every time. The kernel is recycled after a crash, a wedged request (timeout)
// or a fixed request budget; every kill path runs the cursor-restore failsafe
// because the kernel may have died mid-borrow with the pointer hidden.
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createInterface } from 'node:readline';
import { restoreSystemCursor } from './cursor.js';

const PS1_PATH = join(dirname(fileURLToPath(import.meta.url)), 'uia.ps1');
const DEFAULT_TIMEOUT_MS = 45000;
const RECYCLE_AFTER_REQUESTS = 200;
const IDLE_CLOSE_MS = 120000;   // retire the resident kernel when nothing happens

let kernel = null;
let idleTimer = null;
// All kernel traffic is serialized: the serve protocol is strictly one
// in-flight request, and tool calls are effectively sequential anyway.
let chain = Promise.resolve();

function armIdleClose() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    // Idle at the request boundary: no borrow can be mid-flight, so a plain
    // kill is safe and the next call spawns a fresh kernel.
    if (kernel && !kernel.current) {
      try { kernel.proc.kill(); } catch { /* already gone */ }
      kernel = null;
    }
  }, IDLE_CLOSE_MS);
  idleTimer.unref?.();
}

function dropKernel(state, reason, pendingError) {
  if (kernel === state) kernel = null;
  clearTimeout(idleTimer);
  idleTimer = null;
  clearTimeout(state.currentTimer);
  state.currentTimer = null;
  const current = state.current;
  state.current = null;
  try { state.proc.kill(); } catch { /* already gone */ }
  if (current) {
    current.cleanup?.();
    // The kernel may have died mid-borrow with the system cursor hidden.
    restoreSystemCursor();
    current.reject(new Error(`${pendingError}: ${state.stderr.slice(0, 300)}`));
  }
}

function ensureKernel() {
  if (kernel) return kernel;
  const proc = spawn('powershell.exe', [
    '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Sta', '-File', PS1_PATH, '--serve',
  ], {
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  });
  const state = {
    proc,
    served: 0,
    current: null,   // { resolve, reject } of the single in-flight request
    currentTimer: null,
    stderr: '',
  };
  proc.stderr.setEncoding('utf8');
  proc.stderr.on('data', (chunk) => {
    if (state.stderr.length < 8192) state.stderr += chunk;
  });
  proc.stdout.setEncoding('utf8');
  const rl = createInterface({ input: proc.stdout, crlfDelay: Number.POSITIVE_INFINITY });
  rl.on('line', (line) => {
    const trimmed = line.trim();
    if (!trimmed.startsWith('{') || !state.current) return;
    const current = state.current;
    state.current = null;
    clearTimeout(state.currentTimer);
    state.currentTimer = null;
    current.cleanup?.();
    state.served += 1;
    try {
      const obj = JSON.parse(trimmed);
      if (obj && obj.ok === false) current.reject(new Error(obj.error || 'uia.ps1 reported failure'));
      else current.resolve(obj);
    } catch (e) {
      current.reject(new Error(`uia.ps1 bad JSON: ${e.message}; head: ${trimmed.slice(0, 600)}`));
    }
    // Recycle the kernel on a budget so UIA/COM state cannot accumulate forever.
    if (state.served >= RECYCLE_AFTER_REQUESTS && kernel === state) {
      kernel = null;
      try { state.proc.kill(); } catch { /* already gone */ }
      return;
    }
    armIdleClose();
  });
  proc.once('close', () => dropKernel(state, 'exited', 'uia kernel exited before answering'));
  proc.once('error', () => dropKernel(state, 'spawn failed', 'uia kernel could not be spawned'));
  kernel = state;
  return state;
}

function rawCall(params, opts) {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, signal } = opts;
  const state = ensureKernel();
  clearTimeout(idleTimer);   // a request is in flight; the kernel is not idle
  return new Promise((resolve, reject) => {
    let abortHandler = null;
    const current = {
      resolve,
      reject,
      cleanup() {
        if (abortHandler && signal) signal.removeEventListener('abort', abortHandler);
        abortHandler = null;
      },
    };
    const timer = setTimeout(() => {
      // The kernel is wedged (likely a modal on the target window): kill it and
      // let the next call spawn a fresh one.
      dropKernel(state, 'timed out', `uia.ps1 timed out after ${timeoutMs}ms`);
    }, timeoutMs);
    abortHandler = () => {
      dropKernel(state, 'aborted', 'uia.ps1 aborted');
    };
    state.current = current;
    state.currentTimer = timer;
    if (signal) {
      if (signal.aborted) return abortHandler();
      signal.addEventListener('abort', abortHandler, { once: true });
    }
    state.stderr = '';
    try {
      state.proc.stdin.write(`${JSON.stringify(params)}\n`);
    } catch {
      dropKernel(state, 'stdin unavailable', 'uia kernel stdin is unavailable');
    }
  });
}

/**
 * Invoke the UIA kernel with one JSON action.
 * @param {object} params action body ({ action, app, ... })
 * @param {{timeoutMs?: number, signal?: AbortSignal}} [opts]
 * @returns {Promise<object>} parsed JSON result (ok:true)
 */
export function invokePowerShell(params, opts = {}) {
  const result = chain.then(() => rawCall(params, opts));
  chain = result.then(() => undefined, () => undefined);
  return result;
}

/** Stop the resident kernel immediately (plugin/server teardown). */
export function stopPowerShellKernel() {
  clearTimeout(idleTimer);
  idleTimer = null;
  const state = kernel;
  if (!state) return;
  dropKernel(state, 'stopped', 'uia kernel stopped');
}
