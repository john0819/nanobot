"""Bounded, content-free traces. PG remains the authoritative observation store."""

from typing import Literal

from pydantic import Field

from testpilot.domain import Contract


class TraceRecord(Contract):
    kind: Literal["MODEL_ROUND", "TOOL"]
    duration_ms: float = Field(ge=0, le=3_600_000)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_name: str | None = Field(default=None, pattern=r"^[a-z_]{1,64}$")
    iteration: int = Field(ge=1, le=100)

    def attributes(self) -> dict[str, object]:
        return {"iteration": self.iteration, "tool_name": self.tool_name}
