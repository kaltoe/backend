"""API-02 Idempotency-Key: 10분 안에 같은 키로 다시 오면 처음 201 응답을 그대로 돌려준다.

서버 메모리에만 두므로 서버가 다시 뜨면 비워진다(명세: 서버 메모리 10분).
같은 키 요청이 동시에 와도 회의가 하나만 생기게 키마다 잠근다.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class IdempotencyCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._entries: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        # 기다리는 요청 수를 세어, 마지막 요청이 나갈 때만 잠금을 지운다
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)

    def get(self, key: str, now: float | None = None) -> Any | None:
        self._purge(time.monotonic() if now is None else now)
        entry = self._entries.get(key)
        return entry[1] if entry is not None else None

    def put(self, key: str, value: Any, now: float | None = None) -> None:
        self._entries[key] = ((time.monotonic() if now is None else now) + self.ttl, value)

    def _purge(self, now: float) -> None:
        for key in [k for k, (expires, _) in self._entries.items() if expires <= now]:
            del self._entries[key]

    def clear(self) -> None:
        self._entries.clear()


create_meeting_cache = IdempotencyCache(ttl_seconds=600)
