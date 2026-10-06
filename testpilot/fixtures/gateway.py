"""Local HTTP gateway and upstream fixture with one injectable write-retry defect."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

# These fixed loopback calls must never consult host/system proxy configuration.
_opener = build_opener(ProxyHandler({}))


class Gateway:
    def __init__(self, retry_writes: bool = False) -> None:
        self.retry_writes = retry_writes

    def attempts(self, method: str, upstream_statuses: list[int]) -> list[int]:
        sent: list[int] = []
        for status in upstream_statuses[:3]:
            sent.append(status)
            if status < 500 or (method != "GET" and not self.retry_writes):
                break
        return sent

    def route(self, path: str) -> str:
        return "private-orders" if path.startswith("/orders/") else "public"

    def rate_limit(self, request_number: int, limit: int) -> int:
        return 429 if request_number > limit else 200


@contextmanager
def running_gateway(retry_writes: bool = False) -> Generator[str, None, None]:
    """Private per-test servers; ports and counters never shared across jobs."""
    policy = Gateway(retry_writes)
    upstream_calls = {"GET": 0, "POST": 0}
    limited_calls = 0

    class UpstreamHandler(BaseHTTPRequestHandler):
        def respond(self) -> None:
            upstream_calls[self.command] += 1
            status = 503 if upstream_calls[self.command] == 1 else 200
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"upstream")

        do_GET = respond  # noqa: N815 - stdlib HTTP handler names
        do_POST = respond  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_url = f"http://127.0.0.1:{upstream.server_port}"

    class GatewayHandler(BaseHTTPRequestHandler):
        def respond(self) -> None:
            nonlocal limited_calls
            if self.path == "/retry":
                statuses: list[int] = []
                for _ in range(3):
                    request = Request(upstream_url, method=self.command)
                    try:
                        with _opener.open(request, timeout=2) as response:
                            status = response.status
                    except HTTPError as error:
                        status = error.code
                        error.close()
                    statuses.append(status)
                    if status < 500 or (self.command != "GET" and not retry_writes):
                        break
                result = {"attempts": statuses}
                status = statuses[-1]
            elif self.path == "/limited":
                limited_calls += 1
                status = policy.rate_limit(limited_calls, 2)
                result = {"request_number": limited_calls}
            else:
                status = 200
                result = {"route": policy.route(self.path)}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        do_GET = respond  # noqa: N815 - stdlib HTTP handler names
        do_POST = respond  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:
            pass

    try:
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    except BaseException:
        upstream.server_close()
        raise
    servers = (upstream, gateway)
    threads = [Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
               for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{gateway.server_port}"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
