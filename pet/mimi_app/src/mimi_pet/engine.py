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
from .collision import WorkArea, clamp_to_bounds, step_fall
from .config import load_config
from .drag_controller import DragConfig, DragHybridController, DragOutput
from .frame_player import FramePlayer, FrameSample
from .live_controller import LiveConfig, LiveController, LiveParameters
from .scheduler import PerformanceScheduler
from .state_machine import Event, PetState, PetStateMachine

# Full-frame actions whose faces are explicitly drawn (closed eyes, face
# covering, drinking, eating, sneezing...). Live whole-face switching must
# never overwrite them.
EXPRESSION_LOCKED_ACTIONS = frozenset(
    {
        "fun_hot_tea",
        "fun_meal_break",
        "fun_facepalm",
        "fun_shy_peek",
        "fun_sneeze",
        "fun_hug_whale_plush",
        "fun_reading_drowse",
        "fun_cyber_fortune",
        "fun_proud_smug",
    }
)

BLINK_DURATION_S = 0.14

# Ambience pool for the random trigger: everything else has a scenario or
# AI-event trigger and must not fire randomly.
AMBIENCE_ACTIONS = frozenset(
    {
        "fun_sneeze",
        "fun_shy_peek",
        "fun_cyber_fortune",
        "fun_travel_planner",
        "fun_selfie_pose",
    }
)

# Fortune-teller bubble lines (mostly good, always "for fun").
FORTUNE_TEXTS = (
    "今日运势：大吉！",
    "好运正在向你靠近～",
    "今日宜微笑，忌熬夜～",
    "幸运色是蓝色，仅供娱乐哦",
    "诸事顺遂！仅供娱乐～",
    "前方有小惊喜，仅供娱乐哦",
)


@dataclass(frozen=True)
class EngineFrame:
    state: PetState
    sprite: FrameSample | None
    live: LiveParameters | None = None
    drag: DragOutput | None = None
    expression: str = "neutral"
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

    def __init__(self, library: ActionLibrary, config: dict | None = None) -> None:
        config = config or load_config()
        self.library = library
        self.states = PetStateMachine()
        self.player = FramePlayer()
        live_cfg = config["live"]
        self.live = LiveController(
            LiveConfig(
                breath_period_s=float(live_cfg.get("breath_period_s", 3.8)),
                breath_amplitude=float(live_cfg.get("breath_amplitude", 0.65)),
                gaze_smoothing=float(live_cfg.get("gaze_smoothing", 0.18)),
                max_head_angle_deg=float(live_cfg.get("max_head_angle_deg", 4.0)),
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
        self._after_landing_tidy_probability = float(
            scheduler_cfg.get("after_landing_tidy_probability", 0.35)
        )
        scenario = config.get("scenario", {})
        self.scenario_cfg = scenario
        self._last_input_s = 0.0
        self._last_fortune_day: str | None = None
        physics = config.get("physics", {})
        self.gravity = float(physics.get("gravity_px_s2", 2000.0))
        self.air_drag = float(physics.get("air_drag_per_s", 0.8))
        self.max_fall_speed = float(physics.get("max_fall_speed_px_s", 2800.0))
        self.grounded_epsilon = float(physics.get("grounded_epsilon_px", 1.0))
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

        # Expression state.
        self.expression = "neutral"
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
        # Placement mode: docked (default) falls back to the desk bottom;
        # free placement keeps the character where it was released.
        self.free_placement = False

    # ------------------------------------------------------------------ placement

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
        self.states.dispatch(Event.PERFORM)
        self.player.play(self.library.get(action_id))
        return True

    def force_perform(self, action_id: str) -> bool:
        """Debug/menu path: start from Idle or replace the current performance."""
        state = self.states.state
        if state in (PetState.DRAGGING, PetState.FALLING, PetState.LANDING, PetState.CLOSED):
            return False
        if state is PetState.IDLE:
            self.states.dispatch(Event.PERFORM)
        self.player.play(self.library.get(action_id))
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
        # Dragging always cancels any running full-frame performance.
        self.player.stop()
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
        """Pick a scenario-driven performance; returns True when one started.

        Every action gets a sensible trigger: meal windows, idle durations,
        night hours, low-probability ambience, once-a-day fortune. The rest
        (AI-event actions) are reserved for the future AI system and are only
        playable from the developer menu.
        """
        if self.states.state is not PetState.IDLE:
            return False
        value = random.random() if random_value is None else random_value
        cfg = self.scenario_cfg
        idle_min = self.idle_seconds(now_s) / 60.0
        hour = int(time.localtime(now_s).tm_hour)

        def in_window(hours: list) -> bool:
            for start, end in hours:
                if start <= hour < end:
                    return True
            return False

        meal_windows = []
        for item in cfg.get("meal_windows", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                meal_windows.append(
                    (int(str(item[0]).split(":")[0]), int(str(item[1]).split(":")[0]))
                )
        meal_ok = any(start <= hour < end for start, end in meal_windows)
        if meal_ok and value < float(cfg.get("meal_probability", 0.3)):
            return self.perform("fun_meal_break")

        night_ok = False
        night_hours = cfg.get("night_drowse_hours", [22, 6])
        if len(night_hours) == 2 and night_hours[0] >= night_hours[1]:
            night_ok = hour >= night_hours[0] or hour < night_hours[1]
        if night_ok or idle_min >= float(cfg.get("idle_drowse_min", 30)):
            if value < 0.5:
                return self.perform("fun_reading_drowse")
        if idle_min >= float(cfg.get("idle_hot_tea_min", 15)):
            if value < 0.4:
                return self.perform("fun_hot_tea")
        if idle_min >= float(cfg.get("idle_hug_min", 20)):
            if value < 0.5:
                return self.perform("fun_hug_whale_plush")

        # Once-a-day fortune (with its speech bubble).
        if cfg.get("fortune_once_per_day", True):
            today = time.strftime("%Y-%m-%d", time.localtime(now_s))
            if self._last_fortune_day != today and value < 0.2:
                self._last_fortune_day = today
                return self.perform("fun_cyber_fortune")

        # Low-probability ambience.
        if value < float(cfg.get("sneeze_probability_per_check", 0.03)):
            return self.perform("fun_sneeze")
        if value < float(cfg.get("shy_peek_probability_per_check", 0.02)):
            return self.perform("fun_shy_peek")
        if value < float(cfg.get("travel_probability_per_check", 0.02)):
            return self.perform("fun_travel_planner")
        if value < float(cfg.get("selfie_probability_per_check", 0.02)):
            return self.perform("fun_selfie_pose")
        return False

    def trigger_click_react(self, random_value: float | None = None) -> None:
        """Short click: soft press-and-rebound squash, plus an occasional
        quick fun reaction (shy peek / sneeze / rock-paper-scissors)."""
        self._click_react_until_s = self.now_s + 0.35
        value = random.random() if random_value is None else random_value
        if (
            self.states.state is PetState.IDLE
            and self.now_s > self._click_fun_until_s
            and value < 0.15
        ):
            # Stable small reactions only (fun_sneeze drifts 29px in its source
            # frames, so it stays out of the click pool).
            choice = random.choice(("fun_shy_peek", "fun_rock_paper_scissors", "fun_cyber_fortune"))
            if self.perform(choice):
                self._click_fun_until_s = self.now_s + 10.0

    def trigger_double_click(self) -> None:
        """Double click: a bigger random reaction (proud / hug / facepalm / tidy)."""
        if self.states.state is not PetState.IDLE:
            return
        choice = random.choice(
            ("fun_proud_smug", "fun_hug_whale_plush", "fun_facepalm", "fun_tidy_dress")
        )
        self.force_perform(choice)

    def trigger_hover_reaction(self) -> None:
        """Hover reaction: the pet notices being watched (shy peek)."""
        if self.states.state is PetState.IDLE:
            self.perform("fun_shy_peek")

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

    def collide_ground(self, ground_y: float) -> None:
        if self.states.state is not PetState.FALLING:
            return
        self.root_y = float(ground_y)
        self.vx = 0.0
        self.vy = 0.0
        self.states.dispatch(Event.GROUND_COLLISION)
        self.player.play(self.library.get("land_recover_v4_12"))

    # ------------------------------------------------------------------ expressions

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
            self.expression = "neutral"
            return
        if self._happy_until_s and now_s >= self._happy_until_s:
            self._happy_until_s = 0.0
        base = "talk" if self.talking else ("happy" if self._happy_until_s else "neutral")
        if now_s >= self._next_blink_s:
            self._blink_until_s = now_s + BLINK_DURATION_S
            low, high = self.blink_interval_s
            self._next_blink_s = now_s + random.uniform(low, high)
        self.expression = "blink" if now_s < self._blink_until_s else base

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
        if cursor_screen is not None:
            self._cursor = (float(cursor_screen[0]), float(cursor_screen[1]))

        if self.states.state is PetState.FALLING:
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
                self.collide_ground(ground_y)  # type: ignore[arg-type]
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
        )
        self._update_expression(now_s, state)

        sample = self.player.advance(dt_s * 1000.0)
        if sample is not None and sample.finished:
            finished_action = self.player.action.id if self.player.action else None
            self.player.stop()
            self.states.dispatch(Event.ACTION_FINISHED)
            if (
                finished_action == "land_recover_v4_12"
                and self.states.state is PetState.IDLE
            ):
                if self._slung_release:
                    # Flung hard -> flustered facepalm after landing.
                    self._slung_release = False
                    self.force_perform("fun_facepalm")
                elif random.random() < self._after_landing_tidy_probability:
                    self.perform("fun_tidy_dress")

        # Fortune bubble while the cyber fortune performance plays.
        if (
            self.states.state is PetState.PERFORMING
            and self.player.action is not None
            and self.player.action.id == "fun_cyber_fortune"
        ):
            if self.bubble_text is None:
                self.bubble_text = random.choice(FORTUNE_TEXTS)
        else:
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
