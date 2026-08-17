#!/usr/bin/env node
/**
 * mimi-desktop-pet 卸载脚本（零依赖，Node 18+）。
 *
 * 从 DSH profile 卸载桌宠插件：
 *   1. 删除 <profile>/node_modules/mimi-desktop-pet
 *   2. 从 profile package.json 的 bundles 移除 mimi-desktop-pet
 *   3. 恢复 profile 的 cordis.patch.yml（移除 mimi-pet insert，还原为 []）
 *
 * 用法：
 *   node uninstall.mjs --profile desktop
 */
import { existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';

const PKG = 'mimi-desktop-pet';
const argv = process.argv.slice(2);
const flagValue = (name) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : undefined;
};

const dshHome = process.env.DSH_HOME || join(homedir(), '.dsh');
const onlyProfile = flagValue('--profile');

const profilesDir = join(dshHome, 'profiles');
const profileNames = onlyProfile
  ? [onlyProfile]
  : existsSync(profilesDir)
    ? readdirSync(profilesDir).filter((n) => existsSync(join(profilesDir, n, 'package.json')))
    : [];

for (const name of profileNames) {
  const profileDir = join(profilesDir, name);

  // 1. remove plugin directory.
  const pluginDir = join(profileDir, 'node_modules', PKG);
  if (existsSync(pluginDir)) {
    rmSync(pluginDir, { recursive: true, force: true });
    console.log(`✔ 已删除插件目录：${name}`);
  }

  // 2. remove from bundles.
  const pkgPath = join(profileDir, 'package.json');
  if (existsSync(pkgPath)) {
    const pkg = JSON.parse(readFileSync(pkgPath, 'utf8'));
    const bundles = pkg?.dsh?.profile?.bundles;
    if (Array.isArray(bundles) && bundles.includes(PKG)) {
      pkg.dsh.profile.bundles = bundles.filter((b) => b !== PKG);
      writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + '\n', 'utf8');
      console.log(`✔ 已从 bundles 移除：${name}`);
    }
  }

  // 3. restore profile patch (drop the mimi-pet insert).
  const patchPath = join(profileDir, 'cordis.patch.yml');
  if (existsSync(patchPath)) {
    const text = readFileSync(patchPath, 'utf8');
    if (text.includes('mimi-pet')) {
      writeFileSync(
        patchPath,
        '# Your patch layer for this dsh profile (restored by uninstall.mjs)\n[]\n',
        'utf8',
      );
      console.log(`✔ 已还原 profile patch：${name}`);
    }
  }
}

console.log('\n重启 dsh 后插件完全卸载（桌宠不再自动启动）。');
