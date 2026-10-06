"""Small deterministic gateway policy fixture with one injectable defect."""


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
