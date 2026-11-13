"""API-03 연속 실패 잠금 (에러 코드 표 TOO_MANY_ATTEMPTS).

- 같은 회의·같은 이름의 비밀번호 5회 연속 실패 → 5분 동안 429
- 같은 회의의 진행자 복구 키 5회 연속 실패 → 5분 동안 429
5번째 실패까지는 401 이고, 그다음 요청부터 잠긴다. 성공하면 횟수를 지운다. 서버 메모리에만 둔다.

검증(bcrypt)은 await 라서, "잠겼나 확인 → 검증 → 실패 기록" 순서면 동시에 온 요청이 모두 확인을 통과한다.
그래서 검증 전에 attempt() 로 시도를 먼저 세고, 성공하면 succeed() 로 지운다.
"""

import time

MAX_FAILURES = 5
LOCK_SECONDS = 300.0


class FailureLock:
    def __init__(self, max_failures: int = MAX_FAILURES, lock_seconds: float = LOCK_SECONDS) -> None:
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        # key → (성공 없이 이어진 시도 수, 잠금이 풀리는 시각)
        self._state: dict[tuple[str, ...], tuple[int, float | None]] = {}

    def attempt(self, key: tuple[str, ...], now: float | None = None) -> bool:
        """시도 하나를 센다. 이미 잠겨 있으면 False(→ 429). await 없이 끝나므로 동시 요청에도 정확하다."""
        t = time.monotonic() if now is None else now
        count, until = self._state.get(key, (0, None))
        if until is not None:
            if t < until:
                return False
            count = 0  # 5분이 지나면 처음부터 다시 센다
        count += 1
        self._state[key] = (count, t + self.lock_seconds if count >= self.max_failures else None)
        return True

    def succeed(self, key: tuple[str, ...]) -> None:
        self._state.pop(key, None)

    def reset(self) -> None:
        self._state.clear()


password_failures = FailureLock()
recovery_key_failures = FailureLock()
