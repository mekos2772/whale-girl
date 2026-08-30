"""npm 插件更新提醒：比较已安装的 mimi-desktop-pet 与 npm registry 最新版。

纯 stdlib（urllib），由集成层在 worker 线程调用，绝不在 Qt 主线程联网。

已安装版本从 DSH 的实际安装点发现（适配 DSH 环境）：
  1. ``~/.dsh/profiles/*/node_modules/mimi-desktop-pet`` （DSH 各 profile 的插件）
  2. ``%APPDATA%\\npm\\node_modules\\mimi-desktop-pet`` （npm 全局安装）
  3. 仓库内的 ``dsh-plugin-mimi``（开发目录，打包后不存在则跳过）
多个来源取最高版本作为“当前已安装”。
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

PLUGIN_NAME = "mimi-desktop-pet"
NPM_REGISTRY_URL = f"https://registry.npmjs.org/{PLUGIN_NAME}/latest"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DSH_HOME = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
NPM_GLOBAL = (
    Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    / "npm"
    / "node_modules"
)
DEV_PLUGIN_DIR = Path(__file__).resolve().parents[3] / "dsh-plugin-mimi"


def parse_version(value: str) -> tuple[tuple[int, int, int], str]:
    """``'0.4.1-rc.1'`` -> ``((0, 4, 1), 'rc.1')``；非数字段按 0。"""
    value = str(value or "").strip()
    pre = ""
    if "-" in value:
        value, pre = value.split("-", 1)
    numbers = []
    for part in value.split("."):
        try:
            numbers.append(int(part))
        except ValueError:
            numbers.append(0)
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers[:3]), pre


def is_newer(latest: str, installed: str) -> bool:
    """Semver 式比较：数值段高则新；数值相同且安装版带预发布段、最新版不带则新。"""
    latest_nums, latest_pre = parse_version(latest)
    installed_nums, installed_pre = parse_version(installed)
    if latest_nums != installed_nums:
        return latest_nums > installed_nums
    # 同数值：正式版 > 预发布版
    return (not bool(latest_pre)) and bool(installed_pre)


def _read_version(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        version = str(data.get("version") or "").strip()
        return version or None
    except (OSError, ValueError):
        return None


def installed_plugin_version() -> str | None:
    """扫描 DSH profiles、npm 全局与开发目录，取最高已安装版本。"""
    versions: list[str] = []
    profiles = DSH_HOME / "profiles"
    if profiles.is_dir():
        for profile in profiles.iterdir():
            path = profile / "node_modules" / PLUGIN_NAME / "package.json"
            version = _read_version(path)
            if version:
                versions.append(version)
    npm_path = NPM_GLOBAL / PLUGIN_NAME / "package.json"
    version = _read_version(npm_path)
    if version:
        versions.append(version)
    dev_path = DEV_PLUGIN_DIR / "package.json"
    version = _read_version(dev_path)
    if version:
        versions.append(version)
    if not versions:
        return None
    ordered = sorted(
        versions, key=lambda v: (parse_version(v)[0], not parse_version(v)[1])
    )
    return ordered[-1]


def latest_plugin_version(timeout: float = 10.0) -> str | None:
    """查询 npm registry 的 latest 版本；网络失败返回 None（静默降级）。"""
    try:
        request = urllib.request.Request(
            NPM_REGISTRY_URL, headers={"User-Agent": BROWSER_UA}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        version = str(data.get("version") or "").strip()
        return version or None
    except Exception:
        return None