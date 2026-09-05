"""UI-independent coordinator: state machine + full-frame player + Live +
drag hybrid + falling physics. A Qt window only consumes tick results.

This module must stay free of any PySide6 import so the plain unittest
runner keeps working on machines without Qt.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from pathlib import Path

from .action_library import ActionLibrary
from .affection import AffectionChange, AffectionStore, AffectionTracker
from .collision import WorkArea, clamp_to_bounds, step_fall
from .config import load_config
from .debug_log import dbg
from .drag_controller import DragConfig, DragHybridController, DragOutput
from .frame_player import FramePlayer, FrameSample
from .live_controller import LiveConfig, LiveController, LiveParameters
from .scheduler import PerformanceScheduler
from .state_machine import Event, PetState, PetStateMachine


def touch_region(x_ratio: float, y_ratio: float) -> str:
    """Body-part hit zone for the v5 standing master.

    Measured from master_action_style_v1.png's alpha envelope: the character
    occupies y 0.40-1.00 of the window, hands/sleeves flare to x < 0.28 /
    x >= 0.72 around y 0.62-0.88, feet occupy y >= 0.88.
    """
    if y_ratio < 0.52:
        return "head"
    if y_ratio < 0.62:
        return "face" if 0.30 <= x_ratio < 0.70 else "head"
    if y_ratio >= 0.88:
        return "foot"
    if x_ratio < 0.28 or x_ratio >= 0.72:
        return "hand"
    return "belly"

# Full-frame actions whose faces are explicitly drawn (closed eyes, face
# covering, drinking, eating, sneezing...). Live whole-face switching must
# never overwrite them.
EXPRESSION_LOCKED_ACTIONS = frozenset(
    {
        "fun_facepalm",
        "feed_bread",
        "feed_refuse",
    }
)

BLINK_DURATION_S = 0.14

# Ambience pool for the random trigger: everything else has a scenario or
# AI-event trigger and must not fire randomly.
AMBIENCE_ACTIONS: frozenset[str] = frozenset()

MOVEMENT_ACTIONS = frozenset({"walk_left", "walk_right"})


@dataclass(frozen=True)
class EngineFrame:
    state: PetState
    sprite: FrameSample | None
    live: LiveParameters | None = None
    drag: DragOutput | None = None
    expression: str = "neutral"
    mouth: str = "neutral"
    eyes_closed: bool = False
    action_id: str | None = None
    root_x: float = 0.0
    root_y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    grab_dx: float = 0.0
    grab_dy: float = 0.0
    ground_y: float | None = None
    drag_pose_path: Path | None = None
    drag_pose_index: int | None = None
    drag_pose_set: str | None = None
    bubble_text: str | None = None
    click_react: float = 0.0


class PetEngine:
    """Combines the state machine, player, Live and drag controllers."""

    def __init__(
        self,
        library: ActionLibrary,
        config: dict | None = None,
        affection: AffectionTracker | None = None,
        affection_store: AffectionStore | None = None,
    ) -> None:
        config = config or load_config()
        self.library = library
        self.affection = affection or AffectionTracker()
        self.affection_store = affection_store
        self.states = PetStateMachine()
        self.player = FramePlayer()
        live_cfg = config["live"]
        self.live = LiveController(
            LiveConfig(
                breath_period_s=float(live_cfg.get("breath_period_s", 3.8)),
                breath_amplitude=float(live_cfg.get("breath_amplitude", 0.65)),
                gaze_smoothing=float(live_cfg.get("gaze_smoothing", 0.18)),
                max_head_angle_deg=float(live_cfg.get("max_head_angle_deg", 4.0)),
                idle_sway_period_s=float(live_cfg.get("idle_sway_period_s", 3.2)),
                idle_sway_period2_s=float(live_cfg.get("idle_sway_period2_s", 7.7)),
                idle_sway_amplitude_deg=float(live_cfg.get("idle_sway_amplitude_deg", 2.0)),
                idle_bob_period_s=float(live_cfg.get("idle_bob_period_s", 4.4)),
                idle_bob_amplitude=float(live_cfg.get("idle_bob_amplitude", 1.2)),
            )
        )
        self.drag = DragHybridController(
            DragConfig(
                velocity_smoothing=float(config["drag"].get("velocity_smoothing", 0.28)),
                dead_zone_px_s=float(config["drag"].get("dead_zone_px_s", 90.0)),
                medium_enter_px_s=float(config["drag"].get("medium_enter_px_s", 260.0)),
                medium_exit_px_s=float(config["drag"].get("medium_exit_px_s", 200.0)),
                fast_enter_px_s=float(config["drag"].get("fast_enter_px_s", 950.0)),
                fast_exit_px_s=float(config["drag"].get("fast_exit_px_s", 800.0)),
                horizontal_bias=float(config["drag"].get("horizontal_bias", 1.15)),
                micro_max_angle_deg=float(config["drag"].get("micro_max_angle_deg", 0.35)),
                spring_strength=float(config["drag"].get("spring_strength", 0.22)),
                spring_damping=float(config["drag"].get("spring_damping", 0.72)),
                long_press_s=float(config["drag"].get("long_press_s", 2.0)),
                drag_start_move_px=float(config["drag"].get("drag_start_move_px", 8.0)),
            )
        )
        scheduler_cfg = config["scheduler"]
        self.scheduler = PerformanceScheduler(
            cooldown_s=float(scheduler_cfg.get("performance_cooldown_s", 45.0)),
            probability=float(scheduler_cfg.get("random_performance_probability", 0.08)),
        )
        scenario = config.get("scenario", {})
        self.scenario_cfg = scenario
        self._last_input_s = 0.0
        physics = config.get("physics", {})
        self.gravity = float(physics.get("gravity_px_s2", 2000.0))
        self.air_drag = float(physics.get("air_drag_per_s", 0.8))
        self.max_fall_speed = float(physics.get("max_fall_speed_px_s", 2800.0))
        self.grounded_epsilon = float(physics.get("grounded_epsilon_px", 1.0))
        self.hard_landing_speed = float(
            physics.get("hard_landing_speed_px_s", 1800.0)
        )
        movement = config.get("movement", {})
        self.walk_speed = float(movement.get("walk_speed_px_s", 92.0))
        self.slow_walk_speed = float(movement.get("slow_walk_speed_px_s", 48.0))
        self.autonomous_walk_probability = float(
            movement.get("autonomous_walk_probability_per_check", 0.06)
        )
        self.autonomous_walk_idle_min_s = float(
            movement.get("autonomous_walk_idle_min_s", 15.0)
        )
        walk_duration = movement.get("autonomous_walk_duration_s", [4.0, 9.0])
        self.autonomous_walk_duration = (float(walk_duration[0]), float(walk_duration[1]))
        feeding = config.get("feeding", {})
        self.satiety = float(feeding.get("initial_satiety", 0.45))
        self.bread_satiety_gain = float(feeding.get("bread_satiety_gain", 0.4))
        self.feed_refuse_threshold = float(feeding.get("refuse_threshold", 0.82))
        self.satiety_decay_per_s = float(
            feeding.get("satiety_decay_per_hour", 0.08)
        ) / 3600.0
        self.display_w = int(config["window"]["width"])
        self.display_h = int(config["window"]["height"])
        self.blink_interval_s = tuple(float(v) for v in config["live"].get("blink_interval_s", [2.8, 6.5]))

        anchor = self.library.get("land_recover_v4_12")
        self.frame_canvas = anchor.canvas
        self.frame_pivot = anchor.ground_pivot
        self.pivot_display_x = self.frame_pivot[0] * self.display_w / self.frame_canvas[0]
        self.pivot_display_y = self.frame_pivot[1] * self.display_h / self.frame_canvas[1]

        # World state: the unified character root node in screen coordinates.
        self.root_x = 0.0
        self.root_y = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.grab_dx = 0.0
        self.grab_dy = 0.0
        self.last_drag: DragOutput | None = None
        self.now_s = 0.0

        # Hybrid drag pose playback (user-approved main poses, manifest only).
        self.drag_pose_player = FramePlayer()
        self._current_pose_set: str | None = None
        self._last_horizontal_direction: str | None = None

        # Expression state. Mouth and eyes are independent channels so states
        # compose (a blink keeps the smile, talking keeps the tracked gaze);
        # ``expression`` stays as the legacy single-string view.
        self._mouth = "neutral"
        self._eyes_closed = False
        self.talking = False
        self._happy_until_s = 0.0
        self._next_blink_s = 1.5
        self._blink_until_s = 0.0
        self._cursor = (0.0, 0.0)
        self.bubble_text: str | None = None
        self._external_bubble: str | None = None
        self._external_bubble_until_s = 0.0
        self._click_react_until_s = 0.0
        self._click_fun_until_s = -1.0
        self._slung_release = False
        self._movement_velocity_x = 0.0
        self._movement_target_x: float | None = None
        self._movement_until_s: float | None = None
        self._waking = False
        self._last_touch_s = -1e9
        self._touch_streak = 0
        self._touch_regions: list[tuple[float, str]] = []
        self._last_satiety_tick_s: float | None = None
        # Welcome-back greeting: the pointer returning after a long absence
        # waves hello (engine clock is monotonic perf_counter seconds).
        interaction = config.get("interaction", {})
        self.welcome_back_after_s = float(interaction.get("welcome_back_after_s", 30.0))
        self.welcome_cooldown_s = float(interaction.get("welcome_cooldown_s", 90.0))
        self._welcome_last_s = -1e9
        # Idle rest ladder: sit after a quiet minute, sleep after a quiet
        # five; direct interaction or DSH activity always interrupts.
        self.sit_after_idle_s = float(interaction.get("sit_after_idle_s", 60.0))
        self.sleep_after_idle_s = float(interaction.get("sleep_after_idle_s", 300.0))
        # Touch combos: a head pat followed by a palm slap within this window
        # celebrates together.
        self.combo_window_s = float(interaction.get("combo_window_s", 3.0))
        # Placement mode: docked (default) falls back to the desk bottom;
        # free placement keeps the character where it was released.
        self.free_placement = False

    # ------------------------------------------------------------- relationship

    @property
    def affection_score(self) -> int:
        return self.affection.score

    @property
    def affection_level(self) -> str:
        return self.affection.level_name

    def affection_snapshot(self):
        """Return the current relationship view without exposing the tracker."""
        return self.affection.snapshot()

    def note_affection(self, source: str, delta: int | None = None) -> AffectionChange:
        """Record a successful interaction and apply only light visual feedback."""
        change = self.affection.record(source, delta, now=time.time())
        if not change.recorded:
            return change
        if self.affection_store is not None:
            self.affection_store.save(self.affection)
        # Every successful interaction gets the existing smile channel.  A
        # stage transition may add a one-off bubble only when no interaction
        # bubble is currently occupying the channel.
        self.set_happy(2.0 if change.applied_delta <= 1 else 3.0)
        if change.stage_changed and self._external_bubble_until_s <= self.now_s:
            self.set_external_bubble(
                f"我们已经{change.level_after}啦～", 3.0
            )
        return change


    def note_chat_affection(self, text: str, delta: int | None = None) -> AffectionChange:
        """Record eligible desktop-pet chat and reuse relationship feedback."""
        change = self.affection.record_chat(text, delta, now=time.time())
        if not change.recorded:
            return change
        if self.affection_store is not None:
            self.affection_store.save(self.affection)
        self.set_happy(2.0 if change.applied_delta <= 1 else 3.0)
        if change.stage_changed and self._external_bubble_until_s <= self.now_s:
            self.set_external_bubble(f"我们已经{change.level_after}啦～", 3.0)
        return change


    def place_at(self, root_x: float, root_y: float) -> None:
        self.root_x = float(root_x)
        self.root_y = float(root_y)

    def visual_center(self) -> tuple[float, float]:
        """Screen point the cursor is measured against for gaze tracking."""
        return (self.root_x, self.root_y - self.display_h * 0.5)

    # ------------------------------------------------------------------ actions

    def perform(self, action_id: str) -> bool:
        """Play an action from Idle only (scheduler path)."""
        if self.states.state is not PetState.IDLE:
            return False
        self._clear_movement()
        self.states.dispatch(Event.PERFORM)
        self.player.play(self.library.get(action_id))
        return True

    def force_perform(self, action_id: str) -> bool:
        """Debug/menu path: start from Idle or replace the current performance."""
        state = self.states.state
        if state in (
            PetState.DRAGGING,
            PetState.FALLING,
            PetState.LANDING,
            PetState.SLEEPING,
            PetState.CLOSED,
        ):
            return False
        self._clear_movement()
        if state is PetState.IDLE:
            self.states.dispatch(Event.PERFORM)
        self.player.play(self.library.get(action_id))
        return True

    def cancel_performance(self, allowed_ids: frozenset[str] | set[str] | None = None) -> bool:
        """Return a matching full-frame performance to Idle immediately.

        Integrations use this to stop their own stale status animation. They
        must pass an allowlist so an external status update can never cancel
        a direct user interaction.
        """
        if self.states.state is not PetState.PERFORMING or self.player.action is None:
            return False
        if allowed_ids is not None and self.player.action.id not in allowed_ids:
            return False
        self.player.stop()
        self._clear_movement()
        self.states.dispatch(Event.ACTION_FINISHED)
        return True

    def _clear_movement(self) -> None:
        self._movement_velocity_x = 0.0
        self._movement_target_x = None
        self._movement_until_s = None

    def start_walk(
        self,
        direction: str,
        *,
        slow: bool = False,
        target_x: float | None = None,
        duration_s: float | None = None,
    ) -> bool:
        """Start a grounded left/right walk driven by the root node.

        The PNG sequence only supplies the gait. Screen displacement is kept
        in one world-space root so authored frames can never drift or resize.
        """
        if direction not in ("left", "right") or self.states.state is not PetState.IDLE:
            return False
        action_id = f"walk_{direction}"
        if not self.perform(action_id):
            return False
        speed = self.slow_walk_speed if slow else self.walk_speed
        self._movement_velocity_x = -speed if direction == "left" else speed
        self._movement_target_x = float(target_x) if target_x is not None else None
        self._movement_until_s = (
            self.now_s + max(0.0, float(duration_s)) if duration_s is not None else None
        )
        return True

    def stop_walk(self) -> bool:
        if (
            self.states.state is not PetState.PERFORMING
            or self.player.action is None
            or self.player.action.id not in MOVEMENT_ACTIONS
        ):
            return False
        self.player.stop()
        self._clear_movement()
        self.states.dispatch(Event.ACTION_FINISHED)
        return True

    def start_sit(self) -> bool:
        return self.perform("sit_down")

    def stand_up(self) -> bool:
        if (
            self.states.state is PetState.PERFORMING
            and self.player.action is not None
            and self.player.action.id in ("sit_down", "sit_idle")
        ):
            self.player.play(self.library.get("stand_up"))
            return True
        return False

    def start_sleep(self) -> bool:
        if self.states.state is not PetState.IDLE:
            return False
        self._clear_movement()
        self.states.dispatch(Event.SLEEP)
        self._waking = False
        self.player.play(self.library.get("sleep_lie_down"))
        return True

    def wake_up(self) -> bool:
        if self.states.state is not PetState.SLEEPING or self._waking:
            return False
        self._waking = True
        self.player.play(self.library.get("wake_up"))
        return True

    def trigger_touch(self, region: str, x_ratio: float = 0.5) -> bool:
        """Play the authored reaction for the clicked body region.

        Regions: head / face / belly / hand / foot (see touch_region). A foot
        click dodges away with a short walk; a hand click high-fives; head
        pat → palm slap inside the combo window celebrates.
        """
        self.note_input(self.now_s)
        self._click_react_until_s = self.now_s + 0.35
        if self.states.state is PetState.SLEEPING:
            return self.wake_up()
        if self.now_s - self._last_touch_s <= 0.8:
            self._touch_streak += 1
        else:
            self._touch_streak = 1
        self._last_touch_s = self.now_s

        self._touch_regions = [
            entry for entry in self._touch_regions if self.now_s - entry[0] <= self.combo_window_s
        ]
        combo = region == "hand" and any(r == "head" for _, r in self._touch_regions)
        self._touch_regions.append((self.now_s, region))

        if region == "foot":
            return self._foot_dodge(x_ratio)
        if combo:
            self._touch_regions.clear()
            dbg("combo head->hand: celebrate")
            self.set_happy(3.5)
            self.set_external_bubble("最喜欢你了！", 3.0)
            if self.states.state is PetState.IDLE:
                succeeded = self.perform("celebrate")
            else:
                succeeded = self.force_perform("celebrate")
            if succeeded:
                self.note_affection("touch_combo")
            return succeeded

        action_id = {
            "head": "head_pat",
            "belly": "belly_ticklish",
            "face": "cheek_poke",
            "hand": "high_five",
        }.get(region, "cheek_poke")
        if self._touch_streak >= 3:
            # Third rapid touch on the same spot: delighted escalation, even
            # while the previous reaction is still playing.
            self._touch_streak = 0
            self._streak_escalation(region)
        if self.is_sitting:
            # Touches pop the pet out of its sit straight into the reaction.
            self.cancel_performance()
        if self.states.state is not PetState.IDLE:
            return False
        if self.perform(action_id):
            # A friendly touch leaves the character smiling afterwards.
            self.set_happy(1.6)
            self.note_affection(f"touch_{region}")
            return True
        return False

    def _foot_dodge(self, x_ratio: float) -> bool:
        """Foot tickle: hop a couple of steps away from the clicked side."""
        if self.states.state is not PetState.IDLE:
            return False
        direction = "right" if x_ratio < 0.5 else "left"
        dbg(f"foot dodge: {direction}")
        self.set_external_bubble("别挠我脚啦～", 2.5)
        return self.start_walk(direction, duration_s=random.uniform(1.6, 2.6))

    def _streak_escalation(self, region: str) -> None:
        texts = {
            "head": "好舒服呀～",
            "face": "脸都要被戳红啦～",
            "belly": "哈哈哈别挠了！",
            "hand": "击掌击掌！",
        }
        self.set_happy(4.0)
        self.set_external_bubble(texts.get(region, "再来一次～"), 3.0)

    def feed_bread(self) -> str | None:
        """Feed Mimi once; the response depends on the persistent satiety."""
        self.note_input(self.now_s)
        if self.states.state is PetState.SLEEPING:
            self.wake_up()
            return None
        if self.is_sitting:
            # Drop the sit instantly so the treat performance can play.
            self.cancel_performance()
        if self.states.state is not PetState.IDLE:
            return None
        if self.satiety >= self.feed_refuse_threshold:
            action_id = "feed_refuse"
            self.set_external_bubble("吃不下啦～", 3.0)
        else:
            action_id = "feed_bread"
            self.satiety = min(1.0, self.satiety + self.bread_satiety_gain)
            self.set_external_bubble("谢谢！好吃～", 3.0)
        if not self.perform(action_id):
            return None
        if action_id == "feed_bread":
            # A accepted treat leaves the character smiling afterwards.
            self.set_happy(3.0)
            self.note_affection("feed_bread")
        return action_id

    def receive_drop(self) -> bool:
        """React when a file or text is dropped onto the pet."""
        self.note_input(self.now_s)
        if self.states.state is PetState.SLEEPING:
            self.wake_up()
            return True
        if self.is_sitting:
            self.cancel_performance()
        if not self.perform("file_drop_receive"):
            return False
        self.set_external_bubble("收到啦！", 3.0)
        self.note_affection("file_drop")
        return True

    def try_random_performance(self, now_s: float, random_value: float | None = None) -> bool:
        if self.states.state is not PetState.IDLE:
            return False
        if now_s - self.scheduler.last_performance_s < self.scheduler.cooldown_s:
            return False
        value = random.random() if random_value is None else random_value
        if value >= self.scheduler.probability:
            return False
        choices = [a for a in self.library.random_performances() if a.id in AMBIENCE_ACTIONS]
        if not choices:
            return False
        action = random.choice(choices)
        self.scheduler.last_performance_s = now_s
        self.perform(action.id)
        return True

    # ------------------------------------------------------------------ dragging

    def begin_drag(self, mouse_x: float, mouse_y: float, timestamp_s: float) -> bool:
        if self.states.state is PetState.CLOSED:
            return False
        self.note_input(timestamp_s)
        # Dragging always cancels any running full-frame performance.
        self.player.stop()
        self._clear_movement()
        self._waking = False
        self.drag_pose_player.stop()
        self._current_pose_set = None
        self._last_horizontal_direction = None
        self.drag.reset(mouse_x, mouse_y, timestamp_s)
        self.grab_dx = mouse_x - self.root_x
        self.grab_dy = mouse_y - self.root_y
        self.states.dispatch(Event.PICK_UP)
        return True

    def update_drag(
        self,
        mouse_x: float,
        mouse_y: float,
        timestamp_s: float,
        bounds: WorkArea | None = None,
    ) -> DragOutput | None:
        if self.states.state is not PetState.DRAGGING:
            return None
        output = self.drag.update(mouse_x, mouse_y, timestamp_s)
        # Keep the press-time anchor offset: the character never snaps to the
        # cursor center.
        self.root_x = mouse_x - self.grab_dx
        self.root_y = mouse_y - self.grab_dy
        # Drag cage: the whole window must stay inside the virtual-desktop
        # bounding rectangle so the pet can never be thrown off screen.
        if bounds is not None:
            self.root_x = clamp_to_bounds(
                self.root_x,
                bounds.x + self.pivot_display_x,
                bounds.right - (self.display_w - self.pivot_display_x),
            )
            self.root_y = clamp_to_bounds(
                self.root_y,
                bounds.y + self.pivot_display_y,
                bounds.bottom - (self.display_h - self.pivot_display_y),
            )
        self.vx = output.velocity_x
        self.vy = output.velocity_y
        self.last_drag = output
        # A light click must not flash a picked-up pose. After real movement,
        # however, hold continues through the dense points down to t00 and then
        # freezes there. There is deliberately no separate stop/rebound frame.
        if output.speed_band == "hold" and self.drag_pose_player.action is None:
            return output
        # Vertical motion reports "nearest_horizontal": reuse the last
        # horizontal direction's directly drawn force point.
        pose_set = output.pose_set
        if pose_set.startswith("drag_nearest_horizontal"):
            direction = self._last_horizontal_direction or "right"
            pose_set = f"drag_{direction}_t{output.pose_index:02d}"
        elif pose_set.startswith("drag_") and output.direction in ("left", "right"):
            self._last_horizontal_direction = output.direction
        if pose_set != self._current_pose_set:
            self._current_pose_set = pose_set
            self.drag_pose_player.play(self.library.drag_pose(pose_set))
        return output

    def set_external_bubble(self, text: str | None, duration_s: float = 6.0) -> None:
        """Bubble from outside systems (DSH progress/questions, etc.)."""
        self._external_bubble = text
        self._external_bubble_until_s = self.now_s + duration_s if text else 0.0

    def set_free_placement(self, enabled: bool) -> None:
        """Free placement: released characters stay where they are (no fall)."""
        self.free_placement = bool(enabled)

    def note_input(self, now_s: float) -> None:
        """Mouse activity: resets the idle timer used by scenario triggers."""
        self._last_input_s = now_s

    def idle_seconds(self, now_s: float) -> float:
        return max(0.0, now_s - self._last_input_s)

    def scenario_check(self, now_s: float, random_value: float | None = None) -> bool:
        """Idle rest ladder: walk → sit → sleep (config-driven thresholds).

        Sitting persists until interrupted; a long enough sit escalates to
        sleep. Waking/standing is interaction-driven, never scenario-driven.
        """
        state = self.states.state
        if state is PetState.SLEEPING:
            return False
        if state is PetState.PERFORMING:
            action_id = self.player.action.id if self.player.action else ""
            if action_id not in ("sit_down", "sit_idle"):
                return False  # busy with a real performance; scenarios wait
            if self.idle_seconds(now_s) >= self.sleep_after_idle_s:
                dbg("scenario: sit escalates to sleep")
                return self._sleep_from_sit()
            return False
        if state is not PetState.IDLE:
            return False
        idle = self.idle_seconds(now_s)
        if idle >= self.sleep_after_idle_s:
            dbg("scenario: idle falls asleep")
            return self.start_sleep()
        if idle >= self.sit_after_idle_s:
            dbg("scenario: idle sits down")
            return self.start_sit()
        value = random.random() if random_value is None else random_value
        if (
            idle >= self.autonomous_walk_idle_min_s
            and value < self.autonomous_walk_probability
        ):
            direction = random.choice(("left", "right"))
            duration = random.uniform(*self.autonomous_walk_duration)
            return self.start_walk(direction, duration_s=duration)
        return False

    @property
    def is_sitting(self) -> bool:
        """Sitting = the sit_down/sit_idle performance holds the body."""
        return self.states.state is PetState.PERFORMING and self.player.action is not None and (
            self.player.action.id in ("sit_down", "sit_idle")
        )

    def _sleep_from_sit(self) -> bool:
        if not self.is_sitting:
            return False
        self.player.stop()
        self.states.dispatch(Event.ACTION_FINISHED)
        return self.start_sleep()

    def interrupt_rest(self) -> None:
        """Direct interaction or DSH activity ends rest: wake or stand."""
        if self.states.state is PetState.SLEEPING:
            self.wake_up()
        elif self.is_sitting:
            self.stand_up()

    def trigger_click_react(self, random_value: float | None = None) -> None:
        """Short click: soft press-and-rebound squash."""
        self._click_react_until_s = self.now_s + 0.35

    def trigger_double_click(self) -> bool:
        """Double click: a clear, repeatable high-five interaction.

        While resting, the first double click spends itself standing up (or
        waking); the next one high-fives.
        """
        if self.states.state is PetState.SLEEPING:
            return self.wake_up()
        if self.states.state not in (PetState.IDLE, PetState.PERFORMING):
            return False
        if self.is_sitting:
            return self.stand_up()
        if self.force_perform("high_five"):
            self.set_happy(2.5)
            self.note_affection("double_click")
            return True
        return False

    def trigger_welcome_back(self, away_s: float) -> bool:
        """The pointer returns after a long absence: wave hello.

        ``away_s`` is measured by the window between leave and enter. The
        engine applies the absence threshold and its own cooldown so a busy
        pointer crossing the pet never spams greetings.
        """
        self.note_input(self.now_s)
        if self.states.state is not PetState.IDLE:
            return False
        if away_s < self.welcome_back_after_s:
            return False
        if self.now_s - self._welcome_last_s < self.welcome_cooldown_s:
            return False
        self._welcome_last_s = self.now_s
        self.set_happy(2.5)
        self.set_external_bubble("你回来啦～", 3.0)
        succeeded = self.perform("wave")
        if succeeded:
            self.note_affection("welcome_back")
        return succeeded

    def set_display_size(self, width: int, height: int) -> None:
        """Resize the window uniformly (window-style scaling)."""
        self.display_w = int(width)
        self.display_h = int(height)
        self.pivot_display_x = self.frame_pivot[0] * self.display_w / self.frame_canvas[0]
        self.pivot_display_y = self.frame_pivot[1] * self.display_h / self.frame_canvas[1]

    def _click_react_value(self, now_s: float) -> float:
        if now_s >= self._click_react_until_s:
            return 0.0
        elapsed = self._click_react_until_s - now_s
        progress = 1.0 - elapsed / 0.35
        return math.sin(progress * math.pi)  # 0 -> 1 -> 0

    def release(self, ground_y: float | None, timestamp_s: float) -> str:
        """End dragging. Returns 'falling', 'landing' or '' when not dragging."""
        if self.states.state is not PetState.DRAGGING:
            return ""
        # Fast horizontal release -> the pet was flung; react after landing.
        self._slung_release = abs(self.vx) > 1900.0
        if self.free_placement:
            # Free placement: no fall; settle in place back to idle.
            self.vx = 0.0
            self.vy = 0.0
            self.states.dispatch(Event.RELEASE_GROUNDED)
            self.states.dispatch(Event.ACTION_FINISHED)
            return "landing"
        airborne = ground_y is None or self.root_y < ground_y - self.grounded_epsilon
        if airborne:
            self.states.dispatch(Event.RELEASE_AIRBORNE)
            return "falling"
        self.root_y = min(self.root_y, float(ground_y))
        self.vx = 0.0
        self.vy = 0.0
        self.states.dispatch(Event.RELEASE_GROUNDED)
        self.player.play(self.library.get("land_recover_v4_12"))
        return "landing"

    def collide_ground(self, ground_y: float, impact_speed: float = 0.0) -> None:
        if self.states.state is not PetState.FALLING:
            return
        self.root_y = float(ground_y)
        self.vx = 0.0
        self.vy = 0.0
        self.states.dispatch(Event.GROUND_COLLISION)
        self.player.play(self.library.get("land_recover_v4_12"))

    # ------------------------------------------------------------------ expressions

    @property
    def expression(self) -> str:
        """Legacy single-string view (blink wins) for debug display/tests."""
        return "blink" if self._eyes_closed else self._mouth

    def set_talking(self, talking: bool) -> None:
        self.talking = bool(talking)

    def set_happy(self, duration_s: float = 3.0) -> None:
        self._happy_until_s = self.now_s + max(0.0, duration_s)

    def _update_expression(self, now_s: float, state: PetState) -> None:
        locked = state in (PetState.FALLING, PetState.LANDING) or (
            state is PetState.PERFORMING
            and self.player.action is not None
            and self.player.action.id in EXPRESSION_LOCKED_ACTIONS
        )
        if locked:
            if now_s >= self._next_blink_s:
                low, high = self.blink_interval_s
                self._next_blink_s = now_s + random.uniform(low, high)
            self._mouth = "neutral"
            self._eyes_closed = False
            return
        if self._happy_until_s and now_s >= self._happy_until_s:
            self._happy_until_s = 0.0
        # The mouth flaps between the reviewed open-mouth plate and the exact
        # neutral master. A held-open mouth reads as a frozen expression;
        # ~6 flaps/s is clear at the 60 Hz render rate without flicker. The
        # blink channel runs independently so a blink never erases the smile.
        if self.talking:
            mouth = "talk" if int(now_s * 12.0) % 2 == 0 else "neutral"
        else:
            mouth = "happy" if self._happy_until_s else "neutral"
        if now_s >= self._next_blink_s:
            self._blink_until_s = now_s + BLINK_DURATION_S
            low, high = self.blink_interval_s
            self._next_blink_s = now_s + random.uniform(low, high)
        self._mouth = mouth
        self._eyes_closed = now_s < self._blink_until_s

    # ------------------------------------------------------------------ tick

    def tick(
        self,
        now_s: float,
        dt_s: float,
        cursor_screen: tuple[float, float] | None = None,
        ground_y: float | None = None,
        x_bounds: tuple[float, float] | None = None,
    ) -> EngineFrame:
        dt_s = max(0.0, min(0.1, dt_s))
        self.now_s = now_s
        if self._last_satiety_tick_s is None:
            self._last_satiety_tick_s = now_s
        else:
            satiety_dt = max(0.0, min(60.0, now_s - self._last_satiety_tick_s))
            self.satiety = max(0.0, self.satiety - satiety_dt * self.satiety_decay_per_s)
            self._last_satiety_tick_s = now_s
        if cursor_screen is not None:
            self._cursor = (float(cursor_screen[0]), float(cursor_screen[1]))

        if (
            self.states.state is PetState.PERFORMING
            and self.player.action is not None
            and self.player.action.id in MOVEMENT_ACTIONS
        ):
            previous_x = self.root_x
            self.root_x += self._movement_velocity_x * dt_s
            reached_target = False
            if self._movement_target_x is not None:
                reached_target = (
                    previous_x <= self._movement_target_x <= self.root_x
                    or self.root_x <= self._movement_target_x <= previous_x
                )
                if reached_target:
                    self.root_x = self._movement_target_x
            if x_bounds is not None:
                low, high = x_bounds
                bounded_x = clamp_to_bounds(self.root_x, low, high)
                reached_target = reached_target or bounded_x != self.root_x
                self.root_x = bounded_x
            timed_out = self._movement_until_s is not None and now_s >= self._movement_until_s
            if reached_target or timed_out:
                self.stop_walk()

        if self.states.state is PetState.FALLING:
            impact_speed = min(self.max_fall_speed, self.vy + self.gravity * dt_s)
            step = step_fall(
                self.root_x,
                self.root_y,
                self.vx,
                self.vy,
                dt_s,
                self.gravity,
                ground_y=ground_y,
                air_drag=self.air_drag,
                max_speed=self.max_fall_speed,
            )
            self.root_x, self.root_y, self.vx, self.vy = step.root_x, step.root_y, step.vx, step.vy
            if step.hit:
                self.collide_ground(ground_y, impact_speed)  # type: ignore[arg-type]
            elif x_bounds is not None:
                # Horizontal cage while falling: never fly off the desktop.
                low, high = x_bounds
                if self.root_x < low:
                    self.root_x = low
                    self.vx = 0.0
                elif self.root_x > high:
                    self.root_x = high
                    self.vx = 0.0

        state = self.states.state
        enabled = state in (PetState.IDLE, PetState.PERFORMING, PetState.DRAGGING)
        center_x, center_y = self.visual_center()
        live = self.live.update(
            now_s,
            self._cursor[0] - center_x,
            self._cursor[1] - center_y,
            dragging=state is PetState.DRAGGING,
            performing=state is PetState.PERFORMING,
            enabled=enabled,
            dt_s=dt_s,
        )
        self._update_expression(now_s, state)

        sample = self.player.advance(dt_s * 1000.0)
        if sample is not None and sample.finished:
            finished_action = self.player.action.id if self.player.action else None
            if finished_action == "sit_down":
                self.player.play(self.library.get("sit_idle"))
                sample = self.player.advance(0.0)
            elif self.states.state is PetState.SLEEPING and finished_action in (
                "sleep_lie_down",
            ):
                self.player.play(self.library.get("sleep_loop"))
                sample = self.player.advance(0.0)
            elif self.states.state is PetState.SLEEPING and finished_action == "wake_up":
                self.player.stop()
                self._waking = False
                self.states.dispatch(Event.WAKE)
                sample = None
            else:
                self.player.stop()
                self._clear_movement()
                self.states.dispatch(Event.ACTION_FINISHED)
                if (
                    finished_action == "land_recover_v4_12"
                    and self.states.state is PetState.IDLE
                ):
                    self._slung_release = False

        self.bubble_text = None

        # External bubble (DSH progress / questions) takes priority and
        # expires after its duration.
        if self._external_bubble is not None:
            if now_s < self._external_bubble_until_s:
                self.bubble_text = self._external_bubble
            else:
                self._external_bubble = None

        drag_pose_sample: FrameSample | None = None
        drag_pose_set: str | None = None
        if self.states.state is PetState.DRAGGING:
            if self.drag_pose_player.action is not None:
                drag_pose_sample = self.drag_pose_player.advance(dt_s * 1000.0)
                drag_pose_set = self._current_pose_set

        return EngineFrame(
            state=self.states.state,
            sprite=sample,
            live=live,
            drag=self.last_drag,
            expression=self.expression,
            mouth=self._mouth,
            eyes_closed=self._eyes_closed,
            action_id=self.player.action.id if self.player.action else None,
            root_x=self.root_x,
            root_y=self.root_y,
            vx=self.vx,
            vy=self.vy,
            grab_dx=self.grab_dx,
            grab_dy=self.grab_dy,
            ground_y=ground_y,
            drag_pose_path=drag_pose_sample.path if drag_pose_sample else None,
            drag_pose_index=drag_pose_sample.index if drag_pose_sample else None,
            drag_pose_set=drag_pose_set,
            bubble_text=self.bubble_text,
            click_react=self._click_react_value(now_s),
        )
