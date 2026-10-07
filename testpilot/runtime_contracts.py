"""Host-enforced controls for a bounded nanobot execution fragment."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RunControls:
    checkpoint: Callable[[dict[str, Any]], Awaitable[None]]
    before_model: Callable[[], Awaitable[None]]
    max_iterations: int
