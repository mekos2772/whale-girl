"""npm 插件更新提醒测试：版本比较、安装点发现、集成层事件流。

网络请求全部被替换，离线可跑。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet import plugin_update as pu  # noqa: E402
from mimi_pet.dsh_bridge import DshEvent  # noqa: E402
from mimi_pet.dsh_integration import DshIntegration  # noqa: E402
from mimi_pet.engine import PetEngine  # noqa: E402
from mimi_pet.config import load_config  # noqa: E402
from mimi_pet.action_library import ActionLibrary  # noqa: E402


def make_engine() -> PetEngine:
    config = load_config()
    library = ActionLibrary(Path(config["asset_manifest"]))
    return PetEngine(library, config)


class VersionCompareTests(unittest.TestCase):
    def test_parse_version(self) -> None:
        self.assertEqual(pu.parse_version("0.4.0"), ((0, 4, 0), ""))
        self.assertEqual(pu.parse_version("0.4.1-rc.1"), ((0, 4, 1), "rc.1"))
        self.assertEqual(pu.parse_version("5"), ((5, 0, 0), ""))
        self.assertEqual(pu.parse_version(""), ((0, 0, 0), ""))

    def test_is_newer(self) -> None:
        self.assertTrue(pu.is_newer("0.5.0", "0.4.0"))
        self.assertTrue(pu.is_newer("0.4.1", "0.4.0"))
        self.assertTrue(pu.is_newer("0.4.10", "0.4.9"))
        self.assertTrue(pu.is_newer("0.4.1", "0.4.1-rc.2"))
        self.assertFalse(pu.is_newer("0.4.0", "0.4.0"))
        self.assertFalse(pu.is_newer("0.4.0", "0.5.0"))
        self.assertFalse(pu.is_newer("0.4.1-rc.2", "0.4.1"))
        self.assertFalse(pu.is_newer("0.4.1-rc.2", "0.4.1-rc.1"))


class InstalledVersionTests(unittest.TestCase):
    def test_picks_highest_from_profiles_and_npm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            profile = home / "profiles" / "web" / "node_modules" / pu.PLUGIN_NAME
            profile.mkdir(parents=True)
            (profile / "package.json").write_text(
                json.dumps({"name": pu.PLUGIN_NAME, "version": "0.4.0"}), encoding="utf-8"
            )
            older = home / "profiles" / "desktop" / "node_modules" / pu.PLUGIN_NAME
            older.mkdir(parents=True)
            (older / "package.json").write_text(
                json.dumps({"name": pu.PLUGIN_NAME, "version": "0.1.0"}), encoding="utf-8"
            )
            npm = home / "npm" / "node_modules" / pu.PLUGIN_NAME
            npm.mkdir(parents=True)
            (npm / "package.json").write_text(
                json.dumps({"name": pu.PLUGIN_NAME, "version": "0.3.5"}), encoding="utf-8"
            )
            originals = (pu.DSH_HOME, pu.NPM_GLOBAL, pu.DEV_PLUGIN_DIR)
            pu.DSH_HOME = home
            pu.NPM_GLOBAL = home / "npm" / "node_modules"
            pu.DEV_PLUGIN_DIR = home / "no-dev-plugin"
            try:
                self.assertEqual(pu.installed_plugin_version(), "0.4.0")
            finally:
                pu.DSH_HOME, pu.NPM_GLOBAL, pu.DEV_PLUGIN_DIR = originals

    def test_none_when_nothing_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            originals = (pu.DSH_HOME, pu.NPM_GLOBAL, pu.DEV_PLUGIN_DIR)
            pu.DSH_HOME = home / "nope"
            pu.NPM_GLOBAL = home / "nope"
            pu.DEV_PLUGIN_DIR = home / "nope"
            try:
                self.assertIsNone(pu.installed_plugin_version())
            finally:
                pu.DSH_HOME, pu.NPM_GLOBAL, pu.DEV_PLUGIN_DIR = originals


class RegistryFetchTests(unittest.TestCase):
    def test_latest_plugin_version_parses_registry(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout=10.0):
            captured["url"] = request.full_url
            captured["ua"] = request.get_header("User-agent")
            class Response:
                def read(self):
                    return b'{"name":"mimi-desktop-pet","version":"0.5.0"}'

                def __exit__(self, *a):
                    return False

                def __enter__(self):
                    return self

            return Response()

        original = pu.urllib.request.urlopen
        pu.urllib.request.urlopen = fake_urlopen
        try:
            self.assertEqual(pu.latest_plugin_version(), "0.5.0")
        finally:
            pu.urllib.request.urlopen = original
        self.assertEqual(captured["url"], pu.NPM_REGISTRY_URL)

    def test_latest_returns_none_on_failure(self) -> None:
        def failing_urlopen(request, timeout=10.0):
            raise OSError("offline")

        original = pu.urllib.request.urlopen
        pu.urllib.request.urlopen = failing_urlopen
        try:
            self.assertIsNone(pu.latest_plugin_version())
        finally:
            pu.urllib.request.urlopen = original


class PluginUpdateFlowTests(unittest.TestCase):
    """集成层：plugin/update 事件 → 气泡与菜单状态。"""

    def _integration(self):
        engine = make_engine()
        integration = DshIntegration(engine)

        class Sink:
            def __init__(self) -> None:
                self.bubbles: list[tuple[str, str]] = []

            def show_bubble(self, text, kind="assistant"):
                self.bubbles.append((kind, text))

            def set_row(self, *a):
                pass

        sink = Sink()
        integration.sink = sink
        return integration, sink

    def _update_event(self, installed, latest):
        return DshEvent(
            method="plugin/update",
            rpc_id="",
            payload={"installed": installed, "latest": latest},
        )

    def test_newer_version_bubbles_and_marks_menu(self) -> None:
        integration, sink = self._integration()
        integration._last_bubble_at = -1e9
        integration.drain_events()  # no pending events
        integration._handle_event(self._update_event("0.4.0", "0.5.0"))
        self.assertIn(("info", "插件更新：v0.5.0 已发布（当前 v0.4.0）"), sink.bubbles)
        self.assertEqual(integration.plugin_update, ("0.4.0", "0.5.0"))
        self.assertEqual(integration.plugin_update_available(), ("0.4.0", "0.5.0"))

    def test_older_or_equal_version_stays_silent(self) -> None:
        integration, sink = self._integration()
        integration._last_bubble_at = -1e9
        integration._handle_event(self._update_event("0.4.0", "0.4.0"))
        self.assertEqual(sink.bubbles, [])
        self.assertIsNone(integration.plugin_update_available())
        integration._handle_event(self._update_event("0.5.0", "0.4.0"))
        self.assertEqual(sink.bubbles, [])
        self.assertIsNone(integration.plugin_update_available())

    def test_broken_payload_is_ignored(self) -> None:
        integration, sink = self._integration()
        integration._handle_event(self._update_event("", "0.5.0"))
        integration._handle_event(self._update_event("0.4.0", ""))
        self.assertEqual(sink.bubbles, [])
        self.assertIsNone(integration.plugin_update)

    def test_link_trigger_checks_once(self) -> None:
        """首次连接触发一次检查，后续连接不再重复。"""
        import mimi_pet.dsh_integration as di

        integration, _ = self._integration()
        calls: list[str] = []
        originals = (di.installed_plugin_version, di.latest_plugin_version)
        di.installed_plugin_version = lambda: calls.append("i") or "0.4.0"
        di.latest_plugin_version = lambda: calls.append("l") or "0.5.0"
        try:
            integration._handle_event(
                DshEvent(method="__link", rpc_id="", payload={"connected": True})
            )
            integration._handle_event(
                DshEvent(method="__link", rpc_id="", payload={"connected": True})
            )
            deadline = time.time() + 5.0
            while time.time() < deadline and len(calls) < 2:
                integration.drain_events()
                time.sleep(0.02)
        finally:
            di.installed_plugin_version, di.latest_plugin_version = originals
        self.assertTrue(integration._update_checked)
        self.assertEqual(len(calls), 2, "两个 __link 只应触发一轮检查")


if __name__ == "__main__":
    unittest.main()