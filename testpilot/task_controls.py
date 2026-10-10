"""Control messages cannot change identity, execution scope, assertions or task budgets."""

from pydantic import Field

from testpilot.domain import Contract


class StateControl(Contract):
    expected_state_version: int = Field(ge=0)


class TaskInput(Contract):
    client_request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    text: str = Field(min_length=1, max_length=2000)
