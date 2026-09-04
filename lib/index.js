/**
 * mimi-desktop-pet — DeepSeek Harness 桌面宠物插件（host 端）。
 *
 * DSH 启动时自动拉起 Mimi 桌宠进程（pythonw -m mimi_pet.main），
 * DSH 退出/插件卸载时回收该进程。设置命名空间 mimiPet：
 *   enabled  —— 是否随 DSH 启动桌宠
 *   petDir   —— 桌宠项目根目录（含 mimi_app/src）
 *   python   —— pythonw.exe 完整路径（留空自动探测）
 *   scale    —— 缩放百分比（1-200，写入桌宠侧可选）
 *
 * 纯 Node 标准库 + schemastery，无外部运行时依赖。
 */
import { spawn } from 'node:child_process';
import { appendFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import z from '@deepseek-ai/schemastery';
import { apply as applyComputerUse } from '@milkuovo/dsh-computer-use';

export const name = 'mimi-pet';
export const inject = [];

/** DSH 0.1.2 browser-session authentication URL (memory only). */
let dshAuthUrl = '';
let dshAuthReady = false;

/** settings 命名空间（小写 kebab-case）。 */
const NAMESPACE = 'mimiPet';

/** settings schema：字段既用于持久化，也作为配置面板的表单描述。 */
const Config = z.object({
  enabled: z.boolean().default(true).description('启用 Mimi 桌宠（DSH 启动时自动运行）'),
  petDir: z.string().default('').description('桌宠项目根目录（含 mimi_app/src）'),
  python: z.string().default('').description('pythonw.exe 完整路径（留空自动探测）'),
  scale: z.number().default(100).description('桌宠缩放百分比（1-200）'),
  computerUseEnabled: z.boolean().default(true).description('启用 Windows 界面观察、点击、输入、滚动和拖动能力'),
  computerUseAskBeforeActions: z.boolean().default(false).description('每个界面动作前都请求 DSH 批准（默认关闭；高风险操作仍由 Mimi 先向用户确认）'),
  computerUseScreenshot: z.boolean().default(true).description('观察界面时附带窗口截图（视觉模型效果更好）'),
  computerUseGrid: z.boolean().default(true).description('在观察截图上显示可点击编号标记'),
});

/** 自动探测 pythonw.exe 的位置。 */
function resolvePython() {
  const localAppData = process.env.LOCALAPPDATA || '';
  const candidates = [
    join(localAppData, 'Programs', 'Python', 'Python311', 'pythonw.exe'),
    join(localAppData, 'Programs', 'Python', 'pythonw.exe'),
    'C:\\Windows\\pyw.exe',
    'pythonw',
  ];
  return candidates.find((c) => c === 'pythonw' || existsSync(c)) || 'pythonw';
}

/** 自动探测桌宠项目目录（npm 安装后无法携带本机路径）。 */
function resolvePetDir() {
  if (process.env.MIMI_PET_DIR) return process.env.MIMI_PET_DIR;
  const home = process.env.USERPROFILE || '';
  const candidates = [
    join(home, 'Desktop', '桌宠'),
    join(home, 'Desktop', 'mimi-desktop-pet'),
    join(home, 'mimi-desktop-pet'),
  ];
  return candidates.find((c) => existsSync(join(c, 'mimi_app', 'src'))) || '';
}

function resolveDshHost() {
  const configured = String(process.env.MIMI_DSH_HOST || '').trim();
  if (/^[^/:]+:\d+$/.test(configured)) return configured;
  const port = Number(process.env.MIMI_DSH_PORT || 3080);
  return Number.isInteger(port) && port > 0 && port <= 65535
    ? `127.0.0.1:${port}`
    : '127.0.0.1:3080';
}

/** 诊断日志：写入 %TEMP%/mimi-pet.log（排查插件未启动桌宠的问题）。 */
function log(msg) {
  try {
    appendFileSync(join(process.env.TEMP || '.', 'mimi-pet.log'), `[${new Date().toISOString()}] ${msg}\n`);
  } catch {
    /* 日志失败不影响主流程 */
  }
}

/**
 * cordis 插件入口。
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {object} [config] - insert 条目携带的 entry config。
 */
export function apply(ctx, config) {
  log('apply() 被调用');
  let settingsScope = null;
  ctx.inject(['connection'], (cctx) => {
    try {
      // The URL contains a one-shot launch token. Keep it only in memory and
      // pass it to the short-lived Python child through its environment.
      dshAuthUrl = cctx.connection.authenticatedUrl(`http://${resolveDshHost()}/`);
      dshAuthReady = Boolean(dshAuthUrl);
      log('DSH 认证 URL 已准备');
    } catch (err) {
      dshAuthUrl = '';
      dshAuthReady = false;
    }
  });
  ctx.inject(['settings'], (sctx) => {
    settingsScope = sctx.settings.register(NAMESPACE, Config, { base: config ?? {} });
    log('settings 命名空间已注册');
  });
  const defaults = Config({});
  const getConfig = () =>
    settingsScope ? settingsScope.get() : { ...defaults, ...(config ?? {}) };

  // Mimi owns the conversation and feedback; the companion owns reliable UI
  // perception and input. Loading it here makes one Mimi install a complete
  // desktop assistant. The companion's duplicate guard keeps old profiles
  // safe when they still list computer-use as a separate bundle.
  ctx.inject(['tools'], (toolsCtx) => {
    const cfg = getConfig();
    if (!cfg.computerUseEnabled) {
      log('computer use 已关闭');
      return;
    }
    applyComputerUse(toolsCtx, {
      askBeforeActions: cfg.computerUseAskBeforeActions,
      maxDepth: 8,
      maxNodes: 400,
      includeScreenshot: cfg.computerUseScreenshot,
      annotate: {
        grid: cfg.computerUseGrid,
        lastPoint: true,
        activate: true,
      },
      audit: true,
      // The companion ships with Mimi; Mimi package updates own its version.
      updateCheck: false,
      fx: { overlay: true, screenshot: false },
    });
    log('computer use 工具已接入');
  });

  /** 当前桌宠子进程（仅一个）。 */
  let child = null;

  const startPet = () => {
    if (child) return;
    const cfg = getConfig();
    if (!cfg.enabled) {
      log('startPet: enabled=false，跳过');
      return;
    }
    const python = cfg.python || config?.python || resolvePython();

    // 1) 用户配置/探测的项目目录（可指向自己的完整项目）；
    // 2) 否则使用随包分发的桌宠本体（pet/mimi_app + pet/assets）。
    let petDir = cfg.petDir || config?.petDir || resolvePetDir();
    let appDir = petDir ? join(petDir, 'mimi_app') : '';
    let assetRoot = petDir || '';
    if (!appDir || !existsSync(join(appDir, 'src'))) {
      const bundled = join(dirname(fileURLToPath(import.meta.url)), '..', 'pet');
      if (existsSync(join(bundled, 'mimi_app', 'src'))) {
        petDir = bundled;
        appDir = join(bundled, 'mimi_app');
        assetRoot = bundled;
        log(`使用随包本体：${appDir}`);
      } else {
        log('未找到桌宠本体（petDir 与包内 pet/ 均缺失）');
        console.warn(
          '[mimi-pet] 未找到桌宠本体。请在 DSH 设置面板 mimiPet.petDir 配置项目目录，' +
            '或确认 npm 包包含 pet/ 目录。',
        );
        return;
      }
    }
    const manifestOk = existsSync(
      join(assetRoot, 'assets', 'characters', 'mimi', 'library_v1', 'manifest.json'),
    );
    log(`startPet: appDir=${appDir} assetRoot=${assetRoot} manifestOk=${manifestOk}`);
    if (!manifestOk) {
      console.warn('[mimi-pet] 素材缺失：assets/characters/mimi/library_v1/manifest.json 不存在');
      return;
    }
    try {
      // authenticatedUrl is delivered by ctx.inject asynchronously; do not
      // launch Python before the one-shot URL is available.
      if (!dshAuthReady) {
        log('startPet: DSH 认证 URL 尚未准备，稍后重试');
        setTimeout(startPet, 1000);
        return;
      }
      const launched = spawn(python, ['-m', 'mimi_pet.main'], {
        cwd: appDir,
        env: {
          ...process.env,
          PYTHONPATH: join(appDir, 'src'),
          MIMI_ASSET_ROOT: assetRoot,
          MIMI_SCALE_PERCENT: String(Math.max(1, Math.min(200, Number(cfg.scale) || 100))),
          ...(dshAuthUrl ? { MIMI_DSH_AUTH_URL: dshAuthUrl } : {}),
        },
        windowsHide: true,
        stdio: 'ignore',
      });
      child = launched;
      // spawn() reports a missing/broken executable asynchronously. Without
      // an error listener Node treats it as an uncaught EventEmitter error
      // and can take the DSH host down with the pet plugin.
      launched.once('error', (err) => {
        log(`spawn 异步失败: ${String(err?.message ?? err)}`);
        console.warn('[mimi-pet] 桌宠启动失败', String(err?.message ?? err));
        if (child === launched) child = null;
      });
      launched.once('exit', (code, signal) => {
        log(`桌宠进程退出 code=${code} signal=${signal}`);
        if (child === launched) child = null;
      });
      log(`桌宠已 spawn pid=${launched.pid}`);
      console.log(`[mimi-pet] 桌宠已启动 pid=${launched.pid} (app=${appDir})`);
    } catch (err) {
      log(`spawn 失败: ${String(err?.message ?? err)}`);
      console.warn('[mimi-pet] 桌宠启动失败', String(err?.message ?? err));
      child = null;
    }
  };

  const stopPet = () => {
    if (!child) return;
    const running = child;
    child = null;
    try {
      running.kill();
    } catch {
      /* 忽略 */
    }
  };

  ctx.effect(
    () => {
      // 等待主循环就绪后拉起桌宠（避免与宿主启动竞争）。
      const timer = setTimeout(startPet, 1500);
      return () => {
        clearTimeout(timer);
        stopPet();
      };
    },
    'mimi-pet: desktop pet lifecycle',
  );
}
