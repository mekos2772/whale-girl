from __future__ import annotations

import random
from dataclasses import dataclass

from .action_library import ActionLibrary, ActionSpec
from .state_machine import PetState


@dataclass
class PerformanceScheduler:
    cooldown_s: float = 45.0
    probability: float = 0.08
    last_performance_s: float = -1e9

    def choose(
        self,
        now_s: float,
        state: PetState,
        library: ActionLibrary,
        random_value: float | None = None,
    ) -> ActionSpec | None:
        if state is not PetState.IDLE or now_s - self.last_performance_s < self.cooldown_s:
            return None
        value = random.random() if random_value is None else random_value
        if value >= self.probability:
            return None
        choices = library.random_performances()
        if not choices:
            return None
        action = random.choice(choices)
        self.last_performance_s = now_s
        return action
