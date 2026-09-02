#!/usr/bin/env node
// MCP stdio server for dsh-computer-use — the official adapter's second shape.
// Exposes the ten Computer Use tools over JSON-RPC 2.0 on stdio, one tool call
// at a time. Zero dependencies; reuses the exact same buildTools the DSH plugin
// registers. The element snapshot persists on disk between one-shot invocations
// so element_index addressing works across separate MCP client processes.
//
// Env: CU_APPROVAL=1 enforces approval gating (default off: the MCP host gates),
//     CU_MAX_DEPTH / CU_MAX_NODES tune the UIA capture budget.
import { readFileSync, writeFileSync, existsSync, renameSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Config } from './lib/index.js';
import { buildTools, stopOverlay } from './lib/tools.js';
import { ComputerUseSession } from './lib/session.js';
import { setAuditEnabled } from './lib/audit.js';
import { stopPowerShellKernel } from './lib/ps1.js';

const SESSION_FILE = process.env.CU_SESSION_FILE || join(tmpdir(), 'dsh-cu-mcp-session.json');
const SHOT_FILE = process.env.CU_SCREENSHOT_FILE || join(tmpdir(), 'dsh-cu-last-shot.png');
const PACKAGE_VERSION = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')).version;

const images = new Map();   // attachmentId -> base64 png
let imageSeq = 0;

const ctx = {
  get(name) {
    if (name === 'systemPrompt') return { section() {} };
    if (name === 'attachments') {
      return {
        saveImages: async (inputs) => inputs.map((input) => {
          const ref = { attachmentId: `cu${++imageSeq}`, mediaType: input.mediaType };
          images.set(ref.attachmentId, input.data.toString('base64'));
          // PNG IHDR: width/height as big-endian u32 at bytes 16/20. Carrying
          // them lets get_app_state record modelScale (=1 here: no downscale
          // in MCP mode) and append the coordinate-space header.
          const png = input.data;
          if (input.mediaType === 'image/png' && png?.length > 24 && png.readUInt32BE(12) === 0x49484452) {
            ref.width = png.readUInt32BE(16);
            ref.height = png.readUInt32BE(20);
          }
          return ref;
        }),
      };
    }
    return undefined;
  },
  tools: { register() {} },
  effect() {},
};

const session = new ComputerUseSession();
const config = Config({
  askBeforeActions: process.env.CU_APPROVAL === '1',
  includeScreenshot: true,
  maxDepth: Number(process.env.CU_MAX_DEPTH) || 14,
  maxNodes: Number(process.env.CU_MAX_NODES) || 900,
  fx: { overlay: true, screenshot: false },
});
setAuditEnabled(config.audit !== false);
const registered = buildTools(ctx, session, config);
const byName = new Map(registered.map((t) => [t.name, t]));
process.on('exit', () => {
  stopOverlay();
  stopPowerShellKernel();
});

const INPUT_SCHEMAS = {
  list_apps: { type: 'object', properties: {} },
  get_app_state: { type: 'object', required: ['app'], properties: { app: { type: 'string' } } },
  click: {
    type: 'object', required: ['app'],
    properties: {
      app: { type: 'string' },
      element_index: { type: 'integer', description: 'Element index from the latest get_app_state snapshot' },
      marker: { type: 'string', description: 'Marker id from the numbered crosshair grid on the latest get_app_state screenshot (e.g. "D6"); snaps to the containing element center. Takes priority over element_index and x/y' },
      x: { type: 'number', description: 'X in screenshot pixels' },
      y: { type: 'number', description: 'Y in screenshot pixels' },
      click_count: { type: 'integer', minimum: 1, maximum: 3 },
      mouse_button: { type: 'string', enum: ['left', 'right', 'middle'] },
    },
  },
  perform_secondary_action: { type: 'object', required: ['app', 'element_index', 'action'], properties: { app: { type: 'string' }, element_index: { type: 'integer' }, action: { type: 'string' } } },
  set_value: { type: 'object', required: ['app', 'element_index', 'value'], properties: { app: { type: 'string' }, element_index: { type: 'integer' }, value: { type: 'string' } } },
  select_text: {
    type: 'object', required: ['app', 'element_index', 'text'],
    properties: {
      app: { type: 'string' }, element_index: { type: 'integer' }, text: { type: 'string' },
      prefix: { type: 'string' }, suffix: { type: 'string' },
      selection: { type: 'string', enum: ['text', 'cursor_before', 'cursor_after'] },
    },
  },
  scroll: {
    type: 'object', required: ['app', 'direction'],
    properties: {
      app: { type: 'string' }, direction: { type: 'string', enum: ['up', 'down', 'left', 'right'] },
      element_index: { type: 'integer' }, pages: { type: 'number', exclusiveMinimum: 0, maximum: 10 }, x: { type: 'number' }, y: { type: 'number' },
    },
  },
  drag: { type: 'object', required: ['app', 'from_x', 'from_y', 'to_x', 'to_y'], properties: { app: { type: 'string' }, from_x: { type: 'number' }, from_y: { type: 'number' }, to_x: { type: 'number' }, to_y: { type: 'number' } } },
  press_key: { type: 'object', required: ['app', 'key'], properties: { app: { type: 'string' }, key: { type: 'string' } } },
  type_text: { type: 'object', required: ['app', 'text'], properties: { app: { type: 'string' }, text: { type: 'string' } } },
};

function persistSession() {
  try {
    const s = session;
    if (!s) return;
    const state = s.state ? { ...s.state, screenshot: undefined } : null;   // drop the megabyte base64
    const temporary = `${SESSION_FILE}.${process.pid}.tmp`;
    writeFileSync(temporary, JSON.stringify({
      state,
      stateApp: s.stateApp,
      observationRequired: s.observationRequired,
      lastScreenPoint: s.lastScreenPoint,
      lastAction: s.lastAction,
      trail: s.trail,
      modelScale: s.modelScale,
      markers: s.markers ?? [],
    }));
    renameSync(temporary, SESSION_FILE);
  } catch { /* session persistence is best-effort */ }
}

function restoreSession() {
  try {
    if (!existsSync(SESSION_FILE) || !session) return;
    const saved = JSON.parse(readFileSync(SESSION_FILE, 'utf8'));
    session.state = saved.state ?? null;
    session.stateApp = saved.stateApp ?? null;
    session.observationRequired = saved.observationRequired === true;
    session.lastScreenPoint = saved.lastScreenPoint ?? null;
    session.lastAction = saved.lastAction ?? null;
    session.trail = saved.trail ?? [];
    // One-shot mode: get_app_state and the click that follows it run in
    // different processes, so the recorded attached/capture ratio must
    // survive or coordinate conversion silently degrades to scale=1.
    if (Number.isFinite(saved.modelScale)) session.modelScale = saved.modelScale;
    // Grid markers must survive too: click({marker}) lands in a different
    // process than the get_app_state that drew the crosshairs.
    session.markers = Array.isArray(saved.markers) ? saved.markers : [];
  } catch { /* start fresh on any corruption */ }
}

restoreSession();

function contentFor(name, args, result) {
  const blocks = [];
  if (name === 'get_app_state') {
    blocks.push({ type: 'text', text: result.treeText });
    const ref = result.screenshotRef;
    if (ref && images.has(ref.attachmentId)) {
      const b64 = images.get(ref.attachmentId);
      images.delete(ref.attachmentId);
      try { writeFileSync(SHOT_FILE, Buffer.from(b64, 'base64')); } catch { /* best-effort */ }
      blocks.push({ type: 'text', text: `[screenshot saved to ${SHOT_FILE}]` });
    }
    return blocks;
  }
  const tool = byName.get(name);
  if (tool?.output?.render) {
    for (const block of tool.output.render(args, result)) {
      if (block.type === 'text') blocks.push({ type: 'text', text: block.text });
    }
  } else {
    blocks.push({ type: 'text', text: JSON.stringify(result) });
  }
  return blocks;
}

async function handle(req) {
  const { id, method, params } = req;
  const reply = (result) => process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id, result })}\n`);
  if (method === 'initialize') {
    return reply({
      protocolVersion: '2024-11-05',
      capabilities: { tools: {} },
      serverInfo: { name: 'dsh-computer-use', version: PACKAGE_VERSION },
    });
  }
  if (method === 'notifications/initialized' || method === 'notifications/cancelled') return;
  if (method === 'ping') return reply({});
  if (method === 'tools/list') {
    return reply({
      tools: [...byName.keys()].map((name) => ({
        name,
        description: byName.get(name).description ?? name,
        inputSchema: INPUT_SCHEMAS[name] ?? { type: 'object', properties: {} },
      })),
    });
  }
  if (method === 'tools/call') {
    const name = params?.name;
    const tool = byName.get(name);
    if (!tool) {
      process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id, error: { code: -32602, message: `unknown tool: ${name}` } })}\n`);
      return;
    }
    try {
      const args = params.arguments ?? {};
      const result = await tool.execute(args, {});
      return reply({ content: contentFor(name, args, result), isError: result?.isError === true });
    } catch (error) {
      return reply({ content: [{ type: 'text', text: String(error?.message ?? error) }], isError: true });
    } finally {
      // Failed mutations also invalidate the snapshot; persist that state so a
      // one-shot caller cannot accidentally resume from a stale observation.
      persistSession();
    }
  }
  process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id, error: { code: -32601, message: `method not found: ${method}` } })}\n`);
}

let buffer = '';
let requestChain = Promise.resolve();

function enqueue(req) {
  requestChain = requestChain
    .then(() => handle(req))
    .catch((error) => {
      process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: req.id ?? null, error: { code: -32603, message: String(error?.message ?? error) } })}\n`);
    });
}

process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  let idx;
  while ((idx = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, idx).trim();
    buffer = buffer.slice(idx + 1);
    if (!line) continue;
    let req;
    try { req = JSON.parse(line); } catch {
      process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'parse error' } })}\n`);
      continue;
    }
    enqueue(req);
  }
});
process.stdin.on('end', async () => {
  // stdio clients commonly close input immediately after sending a one-shot
  // request. Wait for the queued action and its reply before exiting.
  await requestChain;
  persistSession();
  stopOverlay();
  stopPowerShellKernel();
  process.exit(0);
});
