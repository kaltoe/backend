"""API-03 연속 실패 잠금 (에러 코드 표 TOO_MANY_ATTEMPTS).

- 같은 회의·같은 이름의 비밀번호 5회 연속 실패 → 5분 동안 429
- 같은 회의의 진행자 복구 키 5회 연속 실패 → 5분 동안 429
5번째 실패까지는 401 이고, 그다음 요청부터 잠긴다. 성공하면 횟수를 지운다. 서버 메모리에만 둔다.
"""

import time

MAX_FAILURES = 5
LOCK_SECONDS = 300.0


class FailureLock:
    def __init__(self, max_failures: int = MAX_FAILURES, lock_seconds: float = LOCK_SECONDS) -> None:
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        # key → (연속 실패 수, 잠금이 풀리는 시각)
        self._state: dict[tuple[str, ...], tuple[int, float | None]] = {}

    @staticmethod
    def _now(now: float | None) -> float:
        return time.monotonic() if now is None else now

    def is_locked(self, key: tuple[str, ...], now: float | None = None) -> bool:
        state = self._state.get(key)
        if state is None or state[1] is None:
            return False
        if self._now(now) < state[1]:
            return True
        del self._state[key]  # 5분이 지나면 처음부터 다시 센다
        return False

    def fail(self, key: tuple[str, ...], now: float | None = None) -> None:
        count = self._state.get(key, (0, None))[0] + 1
        until = self._now(now) + self.lock_seconds if count >= self.max_failures else None
        self._state[key] = (count, until)

    def succeed(self, key: tuple[str, ...]) -> None:
        self._state.pop(key, None)

    def reset(self) -> None:
        self._state.clear()


password_failures = FailureLock()
recovery_key_failures = FailureLock()
