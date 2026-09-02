"""Relationship score, rate limiting and persistence tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.affection import (  # noqa: E402
    AffectionStore,
    AffectionTracker,
    CHAT_MAX_DAILY_GAIN,
    classify_chat_affection,
    MAX_DAILY_GAIN,
    affection_level,
)


class AffectionTrackerTests(unittest.TestCase):
    def test_default_score_and_stage_boundaries(self) -> None:
        self.assertEqual(AffectionTracker().score, 50)
        self.assertEqual(affection_level(0), ("初识", "初识"))
        self.assertEqual(affection_level(24), ("初识", "初识"))
        self.assertEqual(affection_level(25), ("熟悉", "熟悉"))
        self.assertEqual(affection_level(60), ("亲近", "亲近"))
        self.assertEqual(affection_level(85), ("默契", "默契"))

    def test_chat_classifier_accepts_relationship_phrases(self) -> None:
        for text in ("你好", "谢谢你，辛苦啦", "抱抱", "你真棒", "我回来了"):
            result = classify_chat_affection(text)
            self.assertTrue(result.eligible, text)
            self.assertTrue(result.fingerprint)

    def test_chat_classifier_rejects_commands_short_and_repeated_text(self) -> None:
        for text in ("", "好", "/help", "哈哈哈哈", "好好好", "谢谢谢谢", "帮我写代码"):
            result = classify_chat_affection(text)
            self.assertFalse(result.eligible, text)

    def test_chat_record_is_rate_limited_and_persisted(self) -> None:
        tracker = AffectionTracker(score=0, daily_date="2026-09-02")
        first = tracker.record_chat("  谢谢你，辛苦啦  ", now=100.0, day="2026-09-02")
        duplicate = tracker.record_chat("谢谢你，辛苦啦", now=200.0, day="2026-09-02")
        cooldown = tracker.record_chat("你好呀", now=150.0, day="2026-09-02")
        self.assertTrue(first.recorded)
        self.assertEqual(first.applied_delta, 1)
        self.assertEqual(duplicate.reason, "duplicate")
        self.assertEqual(cooldown.reason, "chat_cooldown")

    def test_chat_daily_cap_is_independent_of_general_daily_gain(self) -> None:
        tracker = AffectionTracker(score=0, daily_date="2026-09-02", daily_gain=2)
        changes = [
            tracker.record_chat(text, now=100.0 + index * 100.0, day="2026-09-02")
            for index, text in enumerate(("你好", "谢谢", "抱抱", "加油"))
        ]
        self.assertEqual([change.recorded for change in changes], [True, True, True, False])
        self.assertEqual(changes[-1].reason, "chat_daily_limit")
        self.assertEqual(tracker.chat_daily_gain, CHAT_MAX_DAILY_GAIN)
        self.assertEqual(tracker.daily_gain, 5)

    def test_chat_state_round_trips_and_old_payload_is_compatible(self) -> None:
        tracker = AffectionTracker(score=50, daily_date="2026-09-02")
        tracker.record_chat("你好", now=100.0, day="2026-09-02")
        restored = AffectionTracker.from_payload(tracker.to_payload())
        duplicate = restored.record_chat("你好", now=10000.0, day="2026-09-02")
        self.assertEqual(duplicate.reason, "duplicate")
        old = AffectionTracker.from_payload({"schema_version": 1, "affection": 55})
        self.assertEqual(old.score, 55)

    def test_record_clamps_and_reports_stage_change(self) -> None:
        tracker = AffectionTracker(score=99, daily_date="2026-09-02")
        change = tracker.record("touch_head", delta=10, now=1.0, day="2026-09-02")
        self.assertEqual(change.applied_delta, 1)
        self.assertEqual(tracker.score, 100)
        self.assertFalse(change.stage_changed)
        tracker = AffectionTracker(score=24, daily_date="2026-09-02")
        change = tracker.record("touch_head", delta=1, now=1.0, day="2026-09-02")
        self.assertTrue(change.stage_changed)
        self.assertEqual(change.level_after, "熟悉")

    def test_per_source_cooldown_and_daily_cap(self) -> None:
        tracker = AffectionTracker(score=0, daily_date="2026-09-02", max_daily_gain=MAX_DAILY_GAIN)
        first = tracker.record("touch_head", delta=2, now=1.0, day="2026-09-02")
        repeat = tracker.record("touch_head", delta=2, now=2.0, day="2026-09-02")
        self.assertTrue(first.recorded)
        self.assertFalse(repeat.recorded)
        self.assertEqual(repeat.reason, "cooldown")
        for index in range(20):
            tracker.record(f"source-{index}", delta=1, now=10.0 + index, day="2026-09-02")
        self.assertEqual(tracker.daily_gain, MAX_DAILY_GAIN)

    def test_new_day_resets_daily_cap(self) -> None:
        tracker = AffectionTracker(score=0, daily_date="2026-09-02", daily_gain=MAX_DAILY_GAIN)
        change = tracker.record("touch_head", delta=1, now=100.0, day="2026-09-03")
        self.assertTrue(change.recorded)
        self.assertEqual(tracker.daily_gain, 1)


class AffectionStoreTests(unittest.TestCase):
    def test_save_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AffectionStore(Path(tmp) / "state.json")
            tracker = AffectionTracker(score=72, daily_date="2026-09-02")
            tracker.record("touch_head", delta=1, now=100.0, day="2026-09-02")
            self.assertTrue(store.save(tracker))
            restored = store.load()
            self.assertEqual(restored.score, 73)
            self.assertEqual(restored.last_interaction, "touch_head")
            self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_corrupt_state_is_quarantined_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            tracker = AffectionStore(path).load()
            self.assertEqual(tracker.score, 50)
            self.assertFalse(path.exists())
            self.assertTrue(list(path.parent.glob("state.corrupt-*.json")))

    def test_invalid_shape_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            self.assertEqual(AffectionStore(path).load().score, 50)
            self.assertTrue(list(path.parent.glob("state.corrupt-*.json")))

    def test_replace_failure_keeps_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = AffectionStore(path)
            old = AffectionTracker(score=61)
            self.assertTrue(store.save(old))
            newer = AffectionTracker(score=80)
            with mock.patch("mimi_pet.affection.os.replace", side_effect=OSError("locked")):
                self.assertFalse(store.save(newer))
            self.assertEqual(store.load().score, 61)

    def test_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"MIMI_STATE_PATH": str(Path(tmp) / "portable.json")}):
                self.assertEqual(AffectionStore.default_path().name, "portable.json")


if __name__ == "__main__":
    unittest.main()
