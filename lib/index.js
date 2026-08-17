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
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import z from '@deepseek-ai/schemastery';

export const name = 'mimi-pet';
export const inject = [];

/** settings 命名空间（小写 kebab-case）。 */
const NAMESPACE = 'mimiPet';

/** settings schema：字段既用于持久化，也作为配置面板的表单描述。 */
const Config = z.object({
  enabled: z.boolean().default(true).description('启用 Mimi 桌宠（DSH 启动时自动运行）'),
  petDir: z.string().default('').description('桌宠项目根目录（含 mimi_app/src）'),
  python: z.string().default('').description('pythonw.exe 完整路径（留空自动探测）'),
  scale: z.number().default(100).description('桌宠缩放百分比（1-200）'),
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

/**
 * cordis 插件入口。
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {object} [config] - insert 条目携带的 entry config。
 */
export function apply(ctx, config) {
  let settingsScope = null;
  ctx.inject(['settings'], (sctx) => {
    settingsScope = sctx.settings.register(NAMESPACE, Config, { base: config ?? {} });
  });
  const defaults = Config({});
  const getConfig = () =>
    settingsScope ? settingsScope.get() : { ...defaults, ...(config ?? {}) };

  /** 当前桌宠子进程（仅一个）。 */
  let child = null;

  const startPet = () => {
    if (child) return;
    const cfg = getConfig();
    if (!cfg.enabled) return;
    const petDir = cfg.petDir || config?.petDir || resolvePetDir();
    if (!petDir || !existsSync(join(petDir, 'mimi_app', 'src'))) {
      console.warn(
        '[mimi-pet] 未找到桌宠项目目录（petDir）。请在本机 ~/Desktop/桌宠 部署，' +
          '或设置环境变量 MIMI_PET_DIR / DSH 设置面板 mimiPet.petDir。',
      );
      return;
    }
    const python = cfg.python || config?.python || resolvePython();
    try {
      child = spawn(python, ['-m', 'mimi_pet.main'], {
        cwd: join(petDir, 'mimi_app'),
        env: { ...process.env, PYTHONPATH: join(petDir, 'mimi_app', 'src') },
        windowsHide: true,
        stdio: 'ignore',
      });
      child.on('exit', () => {
        child = null;
      });
      console.log(`[mimi-pet] 桌宠已启动 pid=${child.pid}`);
    } catch (err) {
      console.warn('[mimi-pet] 桌宠启动失败', String(err?.message ?? err));
      child = null;
    }
  };

  const stopPet = () => {
    if (!child) return;
    try {
      child.kill();
    } catch {
      /* 忽略 */
    }
    child = null;
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
