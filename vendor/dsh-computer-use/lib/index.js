// dsh-computer-use — Codex Computer Use for DSH (Windows).
// Host plugin: registers the ten official Computer Use tools over a PowerShell UIA kernel.
import z from '@deepseek-ai/schemastery';
import { ComputerUseSession } from './session.js';
import { buildTools, stopOverlay, endOverlaySession } from './tools.js';
import { setAuditEnabled } from './audit.js';
import { restoreSystemCursor } from './cursor.js';
import { checkPluginUpdate } from './update.js';
import { stopPowerShellKernel } from './ps1.js';

export const name = 'computer-use';

export const inject = ['tools'];

export const Config = z.object({
  askBeforeActions: z
    .boolean()
    .default(false)
    .description('Ask the user for approval before mutating actions (click, type, press, scroll, drag, set_value, select_text, secondary actions). When enabled, fail-closed: mutating actions are refused if no approval channel is available.'),
  maxDepth: z.number().default(8).description('Maximum UI Automation tree depth captured by get_app_state.'),
  maxNodes: z.number().default(400).description('Maximum UI Automation nodes captured by get_app_state.'),
  includeScreenshot: z.boolean().default(true).description('Capture and attach a window screenshot with get_app_state (requires a vision-capable model).'),
  annotate: z
    .object({
      grid: z.boolean().default(true).description('Draw a numbered crosshair grid on get_app_state screenshots; click({marker}) then targets the element under a marker (UIA-snapped to center).'),
      lastPoint: z.boolean().default(true).description('Draw an amber ring at the last action landing point on get_app_state screenshots for self-verification.'),
      activate: z.boolean().default(true).description('Bring the target window to the foreground before capturing get_app_state screenshots — the capture reads the screen, so an occluded window would otherwise photograph the occluder.'),
    })
    .default({}),
  audit: z
    .boolean()
    .default(true)
    .description('Append metadata-only audit records to ~/.dsh-computer-use/audit/computer-use.jsonl (method, hashed app, byte counts, outcome, duration; never arguments or content).'),
  updateCheck: z
    .boolean()
    .default(true)
    .description('At plugin start, check the npm registry (at most once per 24h) for a newer plugin version and log a reminder. Non-blocking; silent on any failure (offline, registry error).'),
  fx: z
    .object({
      screenshot: z.boolean().default(false).description('Debug-only: bake the software cursor into model-facing screenshots. The official overlay is a separate window and is not captured.'),
      overlay: z.boolean().default(true).description('Play the desktop software-cursor motion before pointer-based interactions.'),
      trail: z.boolean().default(false).description('Debug-only: render past action points when screenshot effects are enabled.'),
      lens: z.boolean().default(false).description('Legacy/debug 3D LensSequence playback; normal clicks leave this disabled.'),
      lensFrame: z.number().default(28).description('Debug screenshot lens frame index (0..44) when lens is enabled.'),
    })
    .default({}),
});

const SYSTEM_PROMPT = `Computer Use (Codex-style desktop automation) is available through these tools:
1. list_apps — discover apps with visible windows.
2. get_app_state({app}) — MUST be called before interacting with an app. Returns the accessibility tree (numbered element indices, every line with frame=[x,y,w,h] in screenshot pixels) plus a screenshot of the window. Screenshot pixels are the coordinate space for click/scroll/drag (x, y).
3. Vision first: locate the target on the screenshot and act by pixel coordinates — click({app, x, y}) works on anything you can see, including canvas, video and content that never appears in the accessibility tree. Any tree line's frame center is also a valid pixel target, so a tree hit can be clicked without hunting the screenshot. Use element_index only when the tree clearly lists a matching element (cheaper and enables pattern-level actions when present).
4. After every action attempt, call get_app_state again to observe and verify the result before the next action. This is enforced: action outputs set observationRequired=true and stale snapshots are rejected. The screenshot self-verifies: an amber ring marks where the last action landed (confirm the effect), and a numbered crosshair grid lets you click({marker: 'D6'}) on visible targets instead of regressing raw pixels — prefer markers, use x/y only in unmarked areas.
5. press_key uses xdotool syntax (super+c means Ctrl+C on Windows).`;

// Mimi can embed this plugin while a profile may still list it as a standalone
// bundle. Guard the process-global tool surface so either loading order yields
// exactly one registration instead of duplicate tool-name failures.
const APPLIED_KEY = Symbol.for('@milkuovo/dsh-computer-use/applied');

export function apply(ctx, config = {}) {
  if (globalThis[APPLIED_KEY]) {
    ctx.logger?.('computer-use')?.info?.('already active; skipped duplicate registration');
    return;
  }
  const owner = {};
  globalThis[APPLIED_KEY] = owner;
  const resolved = Config(config);
  setAuditEnabled(resolved.audit);
  const session = new ComputerUseSession();

  try {
    const tools = buildTools(ctx, session, {
      askBeforeActions: resolved.askBeforeActions,
      maxDepth: resolved.maxDepth,
      maxNodes: resolved.maxNodes,
      includeScreenshot: resolved.includeScreenshot,
      annotate: resolved.annotate,
      fx: resolved.fx,
    });
    for (const tool of tools) {
      ctx.tools.register(tool);
    }
  } catch (error) {
    if (globalThis[APPLIED_KEY] === owner) delete globalThis[APPLIED_KEY];
    throw error;
  }

  // npm update reminder — fire-and-forget; checkPluginUpdate itself never throws.
  if (resolved.updateCheck) {
    void checkPluginUpdate(ctx.logger('computer-use'));
  }

  const systemPrompt = ctx.get('systemPrompt');
  if (systemPrompt?.section) {
    systemPrompt.section({
      name: 'tool:computer-use',
      order: 210,
      text: SYSTEM_PROMPT,
    });
  }

  // Official parity: the overlay session (software cursor + screen-edge glow)
  // ends when the agent TURN ends, not after each action. agent/status idle is
  // the harness's turn-ended signal.
  if (typeof ctx.on === 'function') {
    const disposeStatus = ctx.on('agent/status', ({ status } = {}) => {
      if (status === 'idle') endOverlaySession();
    });
    ctx.effect(
      () => () => {
        if (typeof disposeStatus === 'function') disposeStatus();
      },
      'computer-use: agent/status overlay hook',
    );
  }

  ctx.effect(
    () => () => {
      stopOverlay();
      stopPowerShellKernel();
      session.reset();
      if (globalThis[APPLIED_KEY] === owner) delete globalThis[APPLIED_KEY];
      // Last-resort cursor restore on plugin unload: if any overlay process
      // died without restoring, the user's pointer must not stay hidden.
      restoreSystemCursor();
    },
    'computer-use: session state',
  );
}
