"""Bounded semantic progress: repeated observations and A/B oscillation detection."""

from collections.abc import Sequence

from pydantic import Field

from testpilot.domain import Contract


class ProgressStalledError(RuntimeError):
    pass


class ProgressState(Contract):
    history: tuple[str, ...] = Field(default=(), max_length=4)
    stalled: bool = False


def advance(state: ProgressState, fingerprints: Sequence[str]) -> ProgressState:
    history = list(state.history)
    stalled = state.stalled
    for fingerprint in fingerprints:
        history = (history + [fingerprint])[-4:]
        repeated = len(history) >= 3 and len(set(history[-3:])) == 1
        alternating = len(history) == 4 and history[0] == history[2] and history[1] == history[3] and history[0] != history[1]
        stalled = stalled or repeated or alternating
    return ProgressState(history=tuple(history), stalled=stalled)
