"""Fixed independent oracles; seeded defect never changes the assertions."""

import json
import os
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from .gateway import running_gateway

_opener = build_opener(ProxyHandler({}))


def request(base: str, path: str, method: str = "GET") -> tuple[int, dict[str, object]]:
    try:
        with _opener.open(Request(base + path, method=method), timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        with error:
            return error.code, json.loads(error.read())


def test_read_retry() -> None:
    with running_gateway(os.environ["TESTPILOT_FIXTURE_MODE"] == "retry-write-bug") as base:
        status, body = request(base, "/retry")
        assert status == 200
        assert body["attempts"] == [503, 200]


def test_write_no_retry() -> None:
    with running_gateway(os.environ["TESTPILOT_FIXTURE_MODE"] == "retry-write-bug") as base:
        status, body = request(base, "/retry", "POST")
        assert status == 503
        assert body["attempts"] == [503]


def test_route_priority() -> None:
    with running_gateway() as base:
        status, body = request(base, "/orders/123")
        assert status == 200
        assert body["route"] == "private-orders"


def test_rate_limit() -> None:
    with running_gateway() as base:
        assert [request(base, "/limited")[0] for _ in range(3)] == [200, 200, 429]
