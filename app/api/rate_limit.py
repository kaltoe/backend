"""같은 IP의 입장 요청 제한 (에러 코드 표 TOO_MANY_REQUESTS).

API-01(입장 정보)과 API-03(Sign In)이 한 카운터를 함께 쓴다: 1분에 120회를 넘으면 429.
서버 한 대 메모리 구현이다. 클라이언트 IP는 uvicorn --proxy-headers 가 X-Forwarded-For 로 채운다.
"""

import time
from collections import deque

from fastapi import Request

from app.api.errors import ApiError, ErrorCode

WINDOW_SECONDS = 60.0
LIMIT = 120


class SlidingWindowLimiter:
    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        # shortcut: IP별 기록을 지우지 않는다. 회의 몇 개 규모에서는 작고, 커지면 주기적으로 비운다
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str, now: float | None = None) -> bool:
        """요청 하나를 센다. 제한을 넘으면 False."""
        t = time.monotonic() if now is None else now
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= t - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(t)
        return True

    def reset(self) -> None:
        self._hits.clear()


entry_limiter = SlidingWindowLimiter(LIMIT, WINDOW_SECONDS)


def limit_entry_requests(request: Request) -> None:
    """API-01·API-03 라우트의 dependencies 에 넣는다."""
    ip = request.client.host if request.client else "unknown"
    if not entry_limiter.hit(ip):
        raise ApiError(ErrorCode.TOO_MANY_REQUESTS)
