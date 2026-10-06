"""Fixed independent oracles; seeded defect never changes the assertions."""

import os

from .gateway import Gateway


def gateway() -> Gateway:
    return Gateway(retry_writes=os.environ["TESTPILOT_FIXTURE_MODE"] == "retry-write-bug")


def test_read_retry() -> None:
    assert gateway().attempts("GET", [503, 200]) == [503, 200]


def test_write_no_retry() -> None:
    assert gateway().attempts("POST", [503, 200]) == [503]


def test_route_priority() -> None:
    assert gateway().route("/orders/123") == "private-orders"


def test_rate_limit() -> None:
    assert gateway().rate_limit(3, 2) == 429
