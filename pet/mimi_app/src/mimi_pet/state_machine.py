from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class PetState(Enum):
    IDLE = auto()
    PERFORMING = auto()
    DRAGGING = auto()
    FALLING = auto()
    LANDING = auto()
    SLEEPING = auto()
    CLOSED = auto()


class Event(Enum):
    PERFORM = auto()
    ACTION_FINISHED = auto()
    PICK_UP = auto()
    RELEASE_AIRBORNE = auto()
    RELEASE_GROUNDED = auto()
    GROUND_COLLISION = auto()
    SLEEP = auto()
    WAKE = auto()
    CLOSE = auto()


@dataclass(frozen=True)
class Transition:
    previous: PetState
    event: Event
    current: PetState


class PetStateMachine:
    """Small deterministic state machine independent from UI and rendering."""

    def __init__(self) -> None:
        self.state = PetState.IDLE

    def dispatch(self, event: Event) -> Transition:
        previous = self.state
        if event is Event.CLOSE:
            self.state = PetState.CLOSED
        elif event is Event.PICK_UP and self.state is not PetState.CLOSED:
            self.state = PetState.DRAGGING
        elif self.state is PetState.IDLE:
            if event is Event.PERFORM:
                self.state = PetState.PERFORMING
            elif event is Event.SLEEP:
                self.state = PetState.SLEEPING
        elif self.state is PetState.PERFORMING:
            if event is Event.ACTION_FINISHED:
                self.state = PetState.IDLE
        elif self.state is PetState.DRAGGING:
            if event is Event.RELEASE_AIRBORNE:
                self.state = PetState.FALLING
            elif event is Event.RELEASE_GROUNDED:
                self.state = PetState.LANDING
        elif self.state is PetState.FALLING and event is Event.GROUND_COLLISION:
            self.state = PetState.LANDING
        elif self.state is PetState.LANDING and event is Event.ACTION_FINISHED:
            self.state = PetState.IDLE
        elif self.state is PetState.SLEEPING and event is Event.WAKE:
            self.state = PetState.IDLE
        return Transition(previous, event, self.state)
