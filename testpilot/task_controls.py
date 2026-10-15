"""Control messages cannot change identity, execution scope, assertions or task budgets."""

from typing import Literal

from pydantic import Field

from testpilot.domain import Contract


class StateControl(Contract):
    expected_state_version: int = Field(ge=0)


class TaskInput(Contract):
    client_request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    text: str = Field(min_length=1, max_length=2000)


class InputRequest(Contract):
    question: str = Field(min_length=1, max_length=1000)
    reason: Literal["MISSING_CONTEXT", "ASSERTION_SOURCE", "ENVIRONMENT", "KNOWLEDGE_ACCESS"]


class InputRequiredError(RuntimeError):
    def __init__(self, request: InputRequest) -> None:
        super().__init__("INPUT_REQUIRED")
        self.request = request
