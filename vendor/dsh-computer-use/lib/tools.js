// The ten Codex Computer Use tools, aligned with the official method surface:
// list_apps, get_app_state, click, perform_secondary_action, set_value,
// select_text, scroll, drag, press_key, type_text.
import { invokePowerShell } from './ps1.js';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { restoreSystemCursor } from './cursor.js';
import { auditToolCall } from './audit.js';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const OVERLAY_PS1 = join(dirname(fileURLToPath(import.meta.url)), 'overlay.ps1');

// The overlay is a RESIDENT session (official semantics): it spawns on the
// first pointer action and stays alive BETWEEN actions — the software cursor
// stays parked at the last endpoint and the screen-edge glow keeps breathing —
// until the session ends. The session ends when the agent turn finishes
// (agent/status idle, hooked in index.js), after an idle gap, or on teardown.
// The overlay's stdin EOF also fades it out if this process dies first.
const OVERLAY_HOLD_MS = 12000;

let overlayProc = null;          // resident overlay child (one at a time)
let overlayIdleTimer = null;
let arrivalResolver = null;      // resolves the active play's arrived promise
let arrivalTimer = null;
let overlayGeneration = 0;       // increments per resident session spawn

function settleArrival() {
  if (arrivalTimer) { clearTimeout(arrivalTimer); arrivalTimer = null; }
  if (arrivalResolver) {
    const resolve = arrivalResolver;
    arrivalResolver = null;
    resolve();
  }
}

function armOverlayIdle() {
  if (overlayIdleTimer) clearTimeout(overlayIdleTimer);
  overlayIdleTimer = setTimeout(() => endOverlaySession(), OVERLAY_HOLD_MS);
  overlayIdleTimer.unref?.();
}

function endOverlaySession() {
  if (overlayIdleTimer) { clearTimeout(overlayIdleTimer); overlayIdleTimer = null; }
  settleArrival();
  const proc = overlayProc;
  overlayProc = null;
  if (!proc) return;
  try {
    if (proc.stdin.writable) proc.stdin.write(`${JSON.stringify({ op: 'exit' })}\n`);
    proc.stdin.end();
  } catch { /* best-effort: the close failsafe below still restores */ }
  // If the overlay misses the graceful exit, kill it; either way make sure the
  // user's cursor scheme comes back (the overlay restores on its own clean
  // exit, but kill() skips its ProcessExit handler).
  setTimeout(() => {
    try { proc.kill(); } catch { /* already gone */ }
    restoreSystemCursor();
  }, 1500).unref?.();
}

function ensureOverlayProcess() {
  if (overlayProc) return overlayProc;
  const generation = ++overlayGeneration;
  const proc = spawn('powershell.exe', [
    '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Sta', '-File', OVERLAY_PS1,
  ], {
    stdio: ['pipe', 'pipe', 'ignore'],
    windowsHide: true,
  });
  overlayProc = proc;
  proc.stdout.setEncoding('utf8');
  let stdout = '';
  proc.stdout.on('data', (chunk) => {
    stdout += chunk;
    const lines = stdout.split(/\r?\n/);
    stdout = lines.pop() ?? '';
    for (const line of lines) {
      try {
        if (JSON.parse(line).event === 'arrived') settleArrival();
      } catch { /* ignore non-event output */ }
    }
  });
  proc.once('error', () => { try { proc.kill(); } catch { /* already gone */ } });
  proc.once('close', () => {
    if (overlayProc === proc) {
      overlayProc = null;
      // The overlay owns the cursor borrow for the whole session; if it died
      // without running its own restore (crash, kill), reload the scheme here.
      if (generation === overlayGeneration) restoreSystemCursor();
    }
  });
  return proc;
}

export function stopOverlay() {
  endOverlaySession();
}

export { endOverlaySession };

/** Debug: ask the resident overlay to dump strip/window state to %TEMP%\dsh-cu-overlay-diag.txt. */
export function overlayProbe() {
  try { overlayProc?.stdin.write(`${JSON.stringify({ op: 'probe' })}\n`); } catch { /* best-effort */ }
}

export function buildMotionPoints(previousPoint, targets) {
  const validTargets = targets.filter((point) => (
    point && Number.isFinite(point.x) && Number.isFinite(point.y)
  ));
  return [previousPoint ?? { x: 0, y: 0 }, ...validTargets];
}

/**
 * Start a desktop software-cursor interaction on the resident overlay session.
 * The child reports when the cursor reaches the interaction point; the caller
 * then commits the real UI action so visual motion never trails the mutation.
 * After the commit the session KEEPS running (cursor parked, glow breathing) —
 * endOverlaySession() fades it when the turn ends or the idle gap elapses.
 */
export function playOverlay(points, kind, {
  lens = false,
  fog = true,
  pulse = kind === 'click',
  interactionIndex = points.length - 1,
} = {}) {
  if (overlayIdleTimer) { clearTimeout(overlayIdleTimer); overlayIdleTimer = null; }
  // A previous play that never settled resolves here; the resident session
  // itself survives and accepts the new play command.
  settleArrival();

  const proc = ensureOverlayProcess();
  const payload = { lens, fog, pulse, interactionIndex };
  const settled = new Promise((resolve) => { arrivalResolver = resolve; });

  try {
    if (!proc.playStarted) {
      // The first motion initialises the session (and switches the overlay
      // into resident/serve mode on the PS side).
      proc.playStarted = true;
      proc.stdin.write(`${JSON.stringify({ ...payload, points, kind, serve: true })}\n`);
    } else {
      proc.stdin.write(`${JSON.stringify({ op: 'play', ...payload, points, kind })}\n`);
    }
    arrivalTimer = setTimeout(() => settleArrival(), 6500);
    // Arm the idle timer at PLAY time, not only on commit: if the caller dies
    // between arrival and commit (tool error, host crash), the session would
    // otherwise sit in its commit gate holding the borrowed cursor forever.
    armOverlayIdle();
  } catch {
    settleArrival();
  }
  return {
    arrived: settled,
    commit() {
      try {
        if (proc?.stdin.writable) proc.stdin.write(`${JSON.stringify({ op: 'commit' })}\n`);
      } catch { /* animation is best-effort */ }
      armOverlayIdle();
    },
    cancel() {
      // Keep the session alive: the cursor stays parked for the next action.
      settleArrival();
    },
  };
}

function text(text) {
  return [{ type: 'text', text }];
}

function summary(parts) {
  return text(parts.filter(Boolean).join(' '));
}

const REOBSERVE_TEXT = 'Next: call get_app_state to verify before any other action.';

// Legends appended to treeText when the kernel actually drew the aids.
const GRID_LEGEND =
  "\nGrid: numbered crosshairs are drawn on the screenshot — click({marker:'D6'}) clicks the element under that marker (UIA-snapped to its center). Prefer marker for visible targets; use x/y only for unmarked areas.";
const LAST_POINT_LEGEND =
  '\nAmber ring: where the last action landed — check it after each action to confirm the effect.';
const OBSERVATION_REQUIRED_PROPERTY = {
  observationRequired: {
    type: 'boolean',
    required: true,
    description: 'True after every action attempt. Call get_app_state again before the next action.',
  },
};

const APP_PARAM = {
  type: 'string',
  description:
    'App/process name (chrome, notepad.exe), window title substring, pid:<number>, hwnd:<number>, or a bare PID/HWND number.',
};

function pendingCall(title) {
  return {
    card: 'generic',
    title,
    kind: 'computer-use',
    rawInput: title,
  };
}

/**
 * Rescale tree-line frame=[x,y,w,h] values from capture pixels to the
 * attached-image pixel space the model actually sees and clicks in, so tree
 * coordinates and screenshot coordinates are always one space. The kernel
 * emits frames in window-relative capture pixels; the attachment pipeline may
 * downscale the screenshot (ratio recorded as session.modelScale).
 */
export function scaleTreeFrames(treeText, scale) {
  if (!treeText || !Number.isFinite(scale) || scale === 1) return treeText;
  return treeText.replace(/frame=\[(-?\d+),(-?\d+),(-?\d+),(-?\d+)\]/g,
    (_m, x, y, w, h) => `frame=[${Math.round(Number(x) * scale)},${Math.round(Number(y) * scale)},`
      + `${Math.round(Number(w) * scale)},${Math.round(Number(h) * scale)}]`);
}

/**
 * Build all ten tool definitions.
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {import('./session.js').ComputerUseSession} session
 * @param {object} config plugin config (askBeforeActions, maxDepth, maxNodes,
 *   includeScreenshot, annotate)
 */
export function buildTools(ctx, session, config = {}) {
  const askBeforeActions = config.askBeforeActions !== false;
  const maxDepth = config.maxDepth ?? 8;
  const maxNodes = config.maxNodes ?? 400;
  const includeScreenshot = config.includeScreenshot !== false;
  const annotate = config.annotate ?? {};
  const annotateGrid = annotate.grid !== false;        // numbered crosshair grid on screenshots
  const annotateLastPoint = annotate.lastPoint !== false; // amber ring at the last action point
  const annotateActivate = annotate.activate !== false;   // bring the window forward before capture
  const fx = config.fx ?? {};
  const fxScreenshot = fx.screenshot === true;    // debug-only screenshot compositing
  const fxOverlay = fx.overlay !== false;         // desktop software-cursor animation
  const fxLensFrame = typeof fx.lensFrame === 'number' ? fx.lensFrame : 28;
  const fxTrail = fx.trail === true;
  // The 3D LensSequence is not a click marker. Keep it explicit for asset
  // debugging; normal clicks use the fog/press/ripple drawn by the overlay.
  const fxLens = fx.lens === true;

  async function requireApproval(ctx, exec, toolName, reason) {
    if (!askBeforeActions) return; // explicit opt-out in the plugin config
    const appr = ctx.get('approval');
    const agent = exec?.agent;
    if (!appr || !agent) {
      // Fail closed: with askBeforeActions on, a mutating action without an
      // approval channel must refuse loudly instead of running unapproved.
      throw new Error(
        `${toolName}: askBeforeActions=true but no approval channel is available ` +
        `(${appr ? 'no agent context' : 'approval service not registered'}). ` +
        'Run the action inside an agent turn, or set askBeforeActions:false.',
      );
    }
    const outcome = await appr.request({
      agent,
      toolName,
      callId: exec.callId,
      reason,
    });
    if (outcome !== 'allowed-once') {
      throw new Error(`computer use action not approved (answerer said: ${outcome})`);
    }
  }

  function requireFreshSnapshot(app, toolName) {
    return session.requireReady(app, toolName);
  }

  async function runMutation(operation) {
    try {
      const result = await operation();
      return { ...result, observationRequired: true };
    } finally {
      // Even a failed input may have partially changed the UI. Force a fresh
      // observation before any follow-up action instead of trusting stale
      // element paths, bounds, focus, or marker positions.
      session.invalidate();
    }
  }

  /** Build the fx payload for the next get_app_state screenshot. */
  function fxPayload(pulsePoint = null) {
    if (!fxScreenshot) return { disabled: true };
    const payload = {};
    if (session.lastAction) {
      payload.cursor = { x: session.lastAction.x, y: session.lastAction.y };
      if (pulsePoint) payload.fog = { x: pulsePoint.x, y: pulsePoint.y, pulse: 0.7 };
      else payload.fog = { x: session.lastAction.x, y: session.lastAction.y, pulse: 0 };
      if (fxLens) payload.lens = { x: session.lastAction.x, y: session.lastAction.y, frame: fxLensFrame };
    }
    if (fxTrail && session.trail.length > 1) {
      payload.trail = session.trail.slice(1).map((p) => ({ x: p.x, y: p.y }));
    }
    return payload;
  }

  function elementPoint(index) {
    return index == null ? null : session.elementScreenPoint(index);
  }

  /** Model coordinates arrive in the ATTACHED image space (DSH may downscale);
   *  map them back to the capture space before kernel math. */
  function modelCoordinate(value) {
    if (value == null) return null;
    const scale = session.modelScale || 1;
    return scale === 1 ? Number(value) : Number(value) / scale;
  }

  function coordinatePoint(x, y) {
    return x == null || y == null ? null : session.toScreenPoint(modelCoordinate(x), modelCoordinate(y));
  }

  /**
   * Snap a capture-pixel point (e.g. a grid marker) to the smallest interactive
   * UIA element frame containing it, so click precision does not depend on the
   * grid pitch. Operates on the latest kernel snapshot — the same capture-pixel
   * space the markers were computed in.
   */
  function snapMarkerPoint(cx, cy) {
    const elements = session.state?.elements ?? [];
    // Only button-like elements may host a snap: marker quantization is a few
    // pixels, but a huge interactive container (video player, page background
    // click-through) would snap the click to ITS center — a click in the
    // middle of the screen far from the marker. Cap by min dimension.
    const w = session.state?.window?.bounds;
    const cap = w
      ? Math.max(140, Math.round(0.18 * Math.min(w.width, w.height)))
      : 140;
    let best = null;
    for (const el of elements) {
      if (!el || el.enabled === false) continue;
      const secondaryActions = Array.isArray(el.secondaryActions) ? el.secondaryActions : [];
      const interactive = el.settable === true
        || secondaryActions.some((action) => (
          action === 'Invoke' || action === 'Toggle' || action === 'ExpandCollapse'
          || action === 'SelectionItem' || action === 'ScrollItem'
        ));
      if (!interactive) continue;
      const f = el.frame;
      if (!f || cx < f.x || cx > f.x + f.width || cy < f.y || cy > f.y + f.height) continue;
      const centerX = f.x + f.width / 2;
      const centerY = f.y + f.height / 2;
      if (Math.hypot(centerX - cx, centerY - cy) > cap) continue;
      const area = f.width * f.height;
      if (!best || area < best.area) best = { el, area };
    }
    if (!best) return { point: { x: cx, y: cy }, note: 'no button-like element, clicked marker point' };
    const f = best.el.frame;
    const label = String(best.el.automationId || best.el.name || `${best.el.role}#${best.el.index}`).slice(0, 48);
    return {
      point: { x: Math.round(f.x + f.width / 2), y: Math.round(f.y + f.height / 2) },
      note: `snapped to ${label} center`,
    };
  }

  const byteLen = (value) => {
    try { return Buffer.byteLength(JSON.stringify(value ?? null), 'utf8'); } catch { return 0; }
  };

  // Metadata-only audit (official-style): method, hashed app, byte counts,
  // outcome, duration — never arguments or returned content.
  function withAudit(tool) {
    if (typeof tool.execute !== 'function') return tool;
    return {
      ...tool,
      async execute(args, exec) {
        const startedAt = Date.now();
        try {
          const result = await tool.execute(args, exec);
          auditToolCall({
            method: tool.name,
            app: args?.app,
            inputBytes: byteLen(args),
            outcome: 'ok',
            durationMs: Date.now() - startedAt,
            via: result?.via ?? null,
            resultBytes: byteLen(result),
          });
          return result;
        } catch (error) {
          auditToolCall({
            method: tool.name,
            app: args?.app,
            inputBytes: byteLen(args),
            outcome: /not approved|no approval channel/.test(String(error?.message)) ? 'refused' : 'error',
            durationMs: Date.now() - startedAt,
          });
          throw error;
        }
      },
    };
  }

  /** Run the pointer motion before and during the real interaction. */
  async function withPointerMotion(kind, targets, execute, { pulse = kind === 'click', interactionIndex } = {}) {
    const validTargets = targets.filter((p) => p && Number.isFinite(p.x) && Number.isFinite(p.y));
    // Keep duplicate waypoints: for a drag, index 1 is the press point even when
    // the previous cursor position already equals that point.
    const points = buildMotionPoints(session.lastScreenPoint, validTargets);
    const resolvedInteractionIndex = Math.min(
      interactionIndex ?? points.length - 1,
      points.length - 1,
    );
    const motion = fxOverlay && points.length > 1
      ? playOverlay(points, kind, {
          lens: fxLens,
          fog: true,
          pulse,
          interactionIndex: resolvedInteractionIndex,
        })
      : null;

    if (motion) await motion.arrived;
    try {
      // When the overlay is running it owns hiding the system cursor for the
      // whole animation; otherwise uia.ps1 hides it around the real input.
      const pendingResult = execute(motion);
      motion?.commit();
      const result = await pendingResult;
      const fallback = validTargets.at(-1) ?? null;
      const finalPoint = result && Number.isFinite(result.x) && Number.isFinite(result.y)
        ? { x: result.x, y: result.y }
        : fallback;
      if (finalPoint) session.recordAction(kind, finalPoint.x, finalPoint.y);
      return result;
    } catch (error) {
      motion?.cancel();
      throw error;
    }
  }

  return [
    // ---------------------------------------------------------------- list_apps
    {
      name: 'list_apps',
      description:
        'List the apps on this computer. Returns the apps that currently have visible top-level windows, with window titles, process ids and whether each is the foreground window.',
      parameters: {},
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            ok: { type: 'boolean', required: true },
            apps: {
              type: 'array',
              required: true,
              items: {
                type: 'object',
                additionalProperties: false,
                properties: {
                  hwnd: { type: 'integer' },
                  app: { type: 'string', required: true },
                  pid: { type: 'integer', required: true },
                  title: { type: 'string', required: true },
                  foreground: { type: 'boolean' },
                },
              },
            },
            count: { type: 'integer', required: true },
          },
        },
        render: (_args, value) => {
          const lines = value.apps.map((a) => {
            const fg = a.foreground ? ' (foreground)' : '';
            return `- ${a.app} (pid ${a.pid})${fg}: ${a.title}`;
          });
          return text(`Running apps with windows (${value.count}):\n${lines.join('\n')}`);
        },
      },
      isConcurrencySafe: () => true,
      async execute(args, exec) {
        return invokePowerShell({ action: 'list_apps', signal: exec?.signal });
      },
    },

    // ------------------------------------------------------------ get_app_state
    {
      name: 'get_app_state',
      description:
        `Start an app use session if needed, then get the state of the app's key window and return a screenshot and accessibility tree. ` +
        'Call it before the first action and again after every action attempt; stale snapshots are rejected. ' +
        'Returns the accessibility tree with numbered element indices — every line carries frame=[x,y,w,h] in screenshot pixels, '
        + 'so a tree hit can be clicked directly by its frame center without reading the screenshot — plus a screenshot of the window. ' +
        'Coordinates in click/scroll/drag are screenshot-relative pixels.',
      parameters: { app: APP_PARAM },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            ok: { type: 'boolean', required: true },
            window: {
              type: 'object',
              required: true,
              additionalProperties: false,
              properties: {
                hwnd: { type: 'integer', required: true },
                pid: { type: 'integer', required: true },
                processName: { type: 'string', required: true },
                title: { type: 'string', required: true },
                bounds: {
                  type: 'object',
                  additionalProperties: false,
                  properties: {
                    x: { type: 'integer', required: true },
                    y: { type: 'integer', required: true },
                    width: { type: 'integer', required: true },
                    height: { type: 'integer', required: true },
                  },
                },
                foreground: { type: 'boolean', required: true },
              },
            },
            elementCount: { type: 'integer', required: true },
            truncated: { type: 'boolean' },
            treeText: { type: 'string', required: true },
            screenshotRef: {
              oneOf: [
                { type: 'object', additionalProperties: true },
                { type: 'null' },
              ],
              description: 'Durable image attachment reference when a screenshot was captured.',
            },
          },
        },
        render: (_args, value) => {
          const blocks = [{ type: 'text', text: value.treeText }];
          if (value.screenshotRef) {
            blocks.push({ type: 'image', attachment: value.screenshotRef });
          }
          return blocks;
        },
      },
      async execute(args, exec) {
        // Keep repeated observations bound to the same top-level window. A
        // process-name selector can resolve to a different window between
        // calls in Chrome/Electron; explicit hwnd:<n> is how callers switch.
        const continuationHwnd = session.matchesApp(args.app)
          ? session.state?.window?.hwnd ?? null
          : null;
        const result = await invokePowerShell({
          action: 'get_state',
          app: args.app,
          hwnd: continuationHwnd,
          signal: exec?.signal,
          maxDepth,
          maxNodes,
          includeScreenshot,
          fx: fxPayload(),
          annotate: { grid: annotateGrid, lastPoint: annotateLastPoint, activate: annotateActivate, displayWidth: 1024 },
          lastPoint: continuationHwnd ? session.lastScreenPoint ?? null : null,
        });
        session.store(result, args.app);
        // Kernel markers are capture pixels — stored as-is, never rescaled by
        // modelScale (click({marker}) maps them straight through toScreenPoint).
        session.markers = Array.isArray(result.markers) ? result.markers : [];
        let screenshotRef = null;
        if (includeScreenshot && result.screenshot) {
          const attachments = ctx.get('attachments');
          if (attachments) {
            try {
              const refs = await attachments.saveImages([{
                data: Buffer.from(result.screenshot, 'base64'),
                mediaType: 'image/png',
                name: `computer-use-${result.window.processName}-${Date.now()}.png`,
              }]);
              screenshotRef = refs[0] ?? null;
              // The pipeline may downscale the attachment: the model's pixel
              // coordinates live in the attached-image space, so record the
              // exact attached/capture ratio for coordinate conversion.
              if (screenshotRef && Number.isFinite(screenshotRef.width) && Number.isFinite(screenshotRef.height)
                  && result.screenshotWidth > 0) {
                session.modelScale = Math.min(1, Math.max(0.01, screenshotRef.width / result.screenshotWidth));
                // Express tree frames in the same attached-image pixel space the
                // model reads off the screenshot and uses for click x/y.
                result.treeText = scaleTreeFrames(result.treeText, session.modelScale);
                result.treeText +=
                  `\nScreenshot: ${screenshotRef.width}x${screenshotRef.height} px — tree frame=[x,y,w,h] and`
                  + ' click/scroll/drag x/y are all in these attached-image pixels';
              }
            } catch {
              screenshotRef = null; // attachment unavailable: text-only view still works
            }
          }
        }
        if (includeScreenshot && !screenshotRef) {
          result.treeText += '\nScreenshot unavailable — use the accessibility tree only for this observation.';
        }
        // Only the treeText legends leave the tool; markers/lastPointDrawn stay
        // internal (the execute return value must match the output schema).
        if (session.markers.length > 0) {
          result.treeText += GRID_LEGEND;
        }
        if (result.lastPointDrawn === true) {
          result.treeText += LAST_POINT_LEGEND;
        }
        return {
          ok: true,
          window: result.window,
          elementCount: result.elementCount ?? result.elements?.length ?? 0,
          truncated: result.truncated === true,
          treeText: result.treeText,
          screenshotRef,
        };
      },
      presentCall: () => pendingCall('get_app_state'),
    },

    // ------------------------------------------------------------------- click
    {
      name: 'click',
      description:
        'Click an element (by element_index from the latest get_app_state) or a pixel coordinate in the screenshot. ' +
        'marker (a numbered crosshair id from the latest screenshot grid) takes priority when provided: it clicks the center of the UIA element containing the marker. ' +
        'click_count supports double/triple clicks; mouse_button selects left/right/middle.',
      parameters: {
        app: APP_PARAM,
        click_count: { type: 'integer', description: 'Number of clicks (1..3). Defaults to 1; values outside this range are rejected.' },
        element_index: {
          type: 'integer',
          description: 'Element index from the latest get_app_state snapshot.',
        },
        marker: {
          type: 'string',
          description: 'Marker id from the numbered crosshair grid on the latest get_app_state screenshot (e.g. "D6"). The click snaps to the center of the UIA element containing the marker. Takes priority over element_index and x/y.',
        },
        mouse_button: {
          type: 'string',
          enum: ['left', 'right', 'middle'],
          description: 'Mouse button to click. Defaults to left.',
        },
        x: { type: 'number', description: 'X coordinate in screenshot pixel coordinates.' },
        y: { type: 'number', description: 'Y coordinate in screenshot pixel coordinates.' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            ok: { type: 'boolean', required: true },
            via: { type: 'string' },
            note: { type: 'string' },
            x: { type: 'integer' },
            y: { type: 'integer' },
            ...OBSERVATION_REQUIRED_PROPERTY,
          },
        },
        render: (_args, value) => summary([`Clicked (${value.via ?? 'coordinate'})`, value.x != null ? `at (${value.x}, ${value.y})` : '', REOBSERVE_TEXT]),
      },
      async execute(args, exec) {
        requireFreshSnapshot(args.app, 'click');
        let targetDesc = `at (${args.x}, ${args.y})`;
        if (args.marker != null) targetDesc = `marker ${args.marker}`;
        else if (args.element_index != null) targetDesc = `element ${args.element_index}`;
        let path = null;
        let target = null;
        let kernelPoint = null;   // capture-pixel point for the kernel click x/y
        let snapNote = null;
        if (args.marker != null) {
          if (!Array.isArray(session.markers) || session.markers.length === 0) {
            throw new Error('click: no grid markers in session — call get_app_state first');
          }
          const marker = session.markers.find((m) => m && String(m.id).toLowerCase() === String(args.marker).toLowerCase());
          if (!marker) {
            throw new Error(
              `click: marker "${args.marker}" is not in the latest grid (${session.markers.length} markers) — ` +
              'call get_app_state first',
            );
          }
          // Markers are stored in capture pixels (the kernel click x/y space):
          // never route them through modelCoordinate's modelScale division.
          const snapped = snapMarkerPoint(marker.x, marker.y);
          kernelPoint = snapped.point;
          snapNote = snapped.note;
          target = session.toScreenPoint(kernelPoint.x, kernelPoint.y);
        } else if (args.element_index != null) {
          const element = session.requireElement(args.element_index, 'click');
          path = element.path;
          target = elementPoint(args.element_index);
        } else if (args.x != null && args.y != null) {
          target = coordinatePoint(args.x, args.y);
          kernelPoint = { x: modelCoordinate(args.x), y: modelCoordinate(args.y) };
        } else {
          throw new Error('click requires either marker, element_index, or both x and y');
        }
        await requireApproval(ctx, exec, 'click', `Click ${targetDesc} in ${args.app}`);
        return runMutation(() => withPointerMotion('click', [target], (motion) => invokePowerShell({
            action: 'click',
            app: args.app,
            signal: exec?.signal,
            path,
            snapshot_hwnd: session.state?.window?.hwnd ?? null,
            click_count: args.click_count ?? 1,
            mouse_button: args.mouse_button ?? 'left',
            x: kernelPoint ? kernelPoint.x : modelCoordinate(args.x),
            y: kernelPoint ? kernelPoint.y : modelCoordinate(args.y),
            hide_cursor: !motion,
          }).then((result) => (snapNote ? { ...result, note: snapNote } : result)), { pulse: true }));
      },
      presentCall: () => pendingCall('click'),
    },

    // -------------------------------------------------- perform_secondary_action
    {
      name: 'perform_secondary_action',
      description:
        'Invoke a secondary accessibility action exposed by an element (from the Secondary Actions list in get_app_state, e.g. Invoke, Toggle, ExpandCollapse, SelectionItem, ScrollItem).',
      parameters: {
        app: APP_PARAM,
        element_index: { type: 'integer', required: true, description: 'Element identifier from the latest get_app_state.' },
        action: { type: 'string', required: true, description: 'Secondary accessibility action name.' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            ok: { type: 'boolean', required: true },
            via: { type: 'string' },
            error: { type: 'string' },
            ...OBSERVATION_REQUIRED_PROPERTY,
          },
        },
        render: (_args, value) => summary([`Performed secondary action via ${value.via ?? '?'}`, value.error ? `(${value.error})` : '', REOBSERVE_TEXT]),
      },
      async execute(args, exec) {
        requireFreshSnapshot(args.app, 'perform_secondary_action');
        const path = session.requireElement(args.element_index, 'perform_secondary_action').path;
        await requireApproval(ctx, exec, 'perform_secondary_action', `Perform ${args.action} on element ${args.element_index} in ${args.app}`);
        return runMutation(() => withPointerMotion('perform_secondary_action', [elementPoint(args.element_index)], () => invokePowerShell({
          action: 'perform_secondary_action',
          app: args.app,
          signal: exec?.signal,
          path,
          snapshot_hwnd: session.state?.window?.hwnd ?? null,
          secondary_action: args.action,
        }), { pulse: false }));
      },
    },

    // --------------------------------------------------------------- set_value
    {
      name: 'set_value',
      description:
        'Set the value of a settable accessibility element (a text field, search box, etc.). Uses the ValuePattern when available, otherwise focuses the element and replaces its text.',
      parameters: {
        app: APP_PARAM,
        element_index: { type: 'integer', required: true, description: 'Element identifier from the latest get_app_state.' },
        value: { type: 'string', required: true, description: 'Value to assign.' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            ok: { type: 'boolean', required: true },
            via: { type: 'string' },
            ...OBSERVATION_REQUIRED_PROPERTY,
          },
        },
        render: (_args, value) => summary([`Set value via ${value.via ?? '?'}`, REOBSERVE_TEXT]),
      },
      async execute(args, exec) {
        requireFreshSnapshot(args.app, 'set_value');
        const path = session.requireElement(args.element_index, 'set_value').path;
        await requireApproval(ctx, exec, 'set_value', `Set value of element ${args.element_index} in ${args.app}`);
        return runMutation(() => withPointerMotion('set_value', [elementPoint(args.element_index)], () => invokePowerShell({
          action: 'set_value', app: args.app, path, value: args.value, signal: exec?.signal, snapshot_hwnd: session.state?.window?.hwnd ?? null,
        }), { pulse: false }));
      },
    },

    // ------------------------------------------------------------ select_text
    {
      name: 'select_text',
      description:
        'Select text inside a text element, or place the text cursor before or after it. Provide text exactly as it appears in the accessibility tree. If the text is not unique, provide surrounding prefix or suffix text to disambiguate it.',
      parameters: {
        app: APP_PARAM,
        element_index: { type: 'integer', required: true, description: 'Text element identifier from the latest get_app_state.' },
        text: { type: 'string', required: true, description: 'Target text as shown in the accessibility tree.' },
        prefix: { type: 'string', description: 'Optional text immediately before the target, used to disambiguate repeated matches.' },
        suffix: { type: 'string', description: 'Optional text immediately after the target, used to disambiguate repeated matches.' },
        selection: {
          type: 'string',
          enum: ['text', 'cursor_before', 'cursor_after'],
          description: 'Whether to select the text or place the cursor before or after it. Defaults to text.',
        },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            ok: { type: 'boolean', required: true },
            via: { type: 'string' },
            selection: { type: 'string' },
            note: { type: 'string' },
            ...OBSERVATION_REQUIRED_PROPERTY,
          },
        },
        render: (_args, value) => summary([`Selected text via ${value.via ?? '?'}`, value.note ? `(${value.note})` : '', REOBSERVE_TEXT]),
      },
      async execute(args, exec) {
        requireFreshSnapshot(args.app, 'select_text');
        const path = session.requireElement(args.element_index, 'select_text').path;
        await requireApproval(ctx, exec, 'select_text', `Select "${args.text}" in element ${args.element_index} of ${args.app}`);
        return runMutation(() => withPointerMotion('select_text', [elementPoint(args.element_index)], () => invokePowerShell({
            action: 'select_text',
            app: args.app,
            signal: exec?.signal,
            path,
            snapshot_hwnd: session.state?.window?.hwnd ?? null,
            text: args.text,
            prefix: args.prefix,
            suffix: args.suffix,
            selection: args.selection ?? 'text',
          }), { pulse: false }));
      },
    },

    // ----------------------------------------------------------------- scroll
    {
      name: 'scroll',
      description:
        'Scroll an element in a direction by a number of pages. Coordinates (x/y) are screenshot pixels. Element-centered scrolling is preferred when an element_index is provided.',
      parameters: {
        app: APP_PARAM,
        direction: {
          type: 'string',
          enum: ['up', 'down', 'left', 'right'],
          required: true,
          description: 'Scroll direction.',
        },
        element_index: { type: 'integer', description: 'Element to scroll (uses its center).' },
        pages: { type: 'number', description: 'Number of pages to scroll (greater than 0, at most 10). Fractional values supported. Defaults to 1.' },
        x: { type: 'number', description: 'Optional X screenshot coordinate for the scroll target.' },
        y: { type: 'number', description: 'Optional Y screenshot coordinate for the scroll target.' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            ok: { type: 'boolean', required: true },
            via: { type: 'string' },
            direction: { type: 'string' },
            pages: { type: 'number' },
            x: { type: 'integer' },
            y: { type: 'integer' },
            ...OBSERVATION_REQUIRED_PROPERTY,
          },
        },
        render: (_args, value) => summary([`Scrolled ${value.direction ?? ''}`, value.pages != null ? `${value.pages} pages` : '', `(${value.via ?? 'mouse wheel'})`, REOBSERVE_TEXT]),
      },
      async execute(args, exec) {
        requireFreshSnapshot(args.app, 'scroll');
        let path = null;
        if (args.element_index != null) {
          path = session.requireElement(args.element_index, 'scroll').path;
        }
        const target = elementPoint(args.element_index)
          ?? coordinatePoint(args.x, args.y)
          ?? session.windowScreenPoint();
        await requireApproval(ctx, exec, 'scroll', `Scroll ${args.direction} in ${args.app}`);
        return runMutation(() => withPointerMotion('scroll', [target], (motion) => invokePowerShell({
            action: 'scroll',
            app: args.app,
            signal: exec?.signal,
            path,
            snapshot_hwnd: session.state?.window?.hwnd ?? null,
            direction: args.direction,
            pages: args.pages ?? 1,
            x: modelCoordinate(args.x),
            y: modelCoordinate(args.y),
            hide_cursor: !motion,
          }), { pulse: false }));
      },
    },

    // ------------------------------------------------------------------ drag
    {
      name: 'drag',
      description:
        'Drag from one point to another using screenshot pixel coordinates (from_x/from_y to to_x/to_y).',
      parameters: {
        app: APP_PARAM,
        from_x: { type: 'number', required: true, description: 'Start X coordinate in screenshot pixels.' },
        from_y: { type: 'number', required: true, description: 'Start Y coordinate in screenshot pixels.' },
        to_x: { type: 'number', required: true, description: 'End X coordinate in screenshot pixels.' },
        to_y: { type: 'number', required: true, description: 'End Y coordinate in screenshot pixels.' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            ok: { type: 'boolean', required: true },
            via: { type: 'string' },
            x: { type: 'integer' },
            y: { type: 'integer' },
            ...OBSERVATION_REQUIRED_PROPERTY,
          },
        },
        render: (_args, value) => summary([`Dragged via ${value.via ?? 'mouse_event'}`, REOBSERVE_TEXT]),
      },
      async execute(args, exec) {
        requireFreshSnapshot(args.app, 'drag');
        await requireApproval(ctx, exec, 'drag', `Drag in ${args.app} from (${args.from_x}, ${args.from_y}) to (${args.to_x}, ${args.to_y})`);
        const dragStart = coordinatePoint(args.from_x, args.from_y);
        const dragEnd = coordinatePoint(args.to_x, args.to_y);
        return runMutation(() => withPointerMotion('drag', [dragStart, dragEnd], (motion) => invokePowerShell({
            action: 'drag',
            app: args.app,
            signal: exec?.signal,
            snapshot_hwnd: session.state?.window?.hwnd ?? null,
            from_x: modelCoordinate(args.from_x),
            from_y: modelCoordinate(args.from_y),
            to_x: modelCoordinate(args.to_x),
            to_y: modelCoordinate(args.to_y),
            hide_cursor: !motion,
          }), { pulse: false, interactionIndex: 1 }));
      },
    },

    // -------------------------------------------------------------- press_key
    {
      name: 'press_key',
      description:
        'Press a key or key-combination on the keyboard, including modifier and navigation keys. Supports xdotool `key` syntax, for example `a`, `Return`, `Tab`, `super+c` (Ctrl+C on Windows), `Up`, `Page_Up`, `F5`, `KP_0`.',
      parameters: {
        app: APP_PARAM,
        key: { type: 'string', required: true, description: 'Key or key combination to press (xdotool syntax).' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            ok: { type: 'boolean', required: true },
            via: { type: 'string' },
            key: { type: 'string' },
            events: { type: 'integer' },
            ...OBSERVATION_REQUIRED_PROPERTY,
          },
        },
        render: (_args, value) => summary([`Pressed ${value.key ?? ''}`, REOBSERVE_TEXT]),
      },
      async execute(args, exec) {
        requireFreshSnapshot(args.app, 'press_key');
        await requireApproval(ctx, exec, 'press_key', `Press ${args.key} in ${args.app}`);
        return runMutation(() => invokePowerShell({
          action: 'press_key', app: args.app, key: args.key, signal: exec?.signal,
          snapshot_hwnd: session.state?.window?.hwnd ?? null,
        }));
      },
    },

    // -------------------------------------------------------------- type_text
    {
      name: 'type_text',
      description:
        'Type literal text using keyboard input into the focused control of the target app. Uses clipboard paste for Unicode reliability and restores the previous clipboard content afterwards.',
      parameters: {
        app: APP_PARAM,
        text: { type: 'string', required: true, description: 'Literal text to type.' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            ok: { type: 'boolean', required: true },
            via: { type: 'string' },
            ...OBSERVATION_REQUIRED_PROPERTY,
          },
        },
        render: (_args, value) => summary([`Typed ${JSON.stringify(_args.text)} via ${value.via ?? 'clipboard'}`, REOBSERVE_TEXT]),
      },
      async execute(args, exec) {
        requireFreshSnapshot(args.app, 'type_text');
        await requireApproval(ctx, exec, 'type_text', `Type ${JSON.stringify(args.text)} into ${args.app}`);
        return runMutation(() => invokePowerShell({
          action: 'type_text', app: args.app, text: args.text, signal: exec?.signal,
          snapshot_hwnd: session.state?.window?.hwnd ?? null,
        }));
      },
    },
  ].map(defineTool).map(withAudit);
}
