"""회의룸별 WS-01 연결 목록. API-01·API-04 connectedCount, API-03 "접속 중" 판단의 기준이다.

- 같은 참가자가 여러 기기로 붙을 수 있다(진행자 복구 후 두 기기, 2026-11-13 결정). 4409 는 WS-02 전용
- connectedCount 는 연결 수가 아니라 접속 중인 참가자 수
- 마이크 상태는 연결이 끊겨도 기억한다. 처음 들어온 참가자는 ON

서버 한 대 기준의 메모리 구현이다. 서버를 여러 대로 늘리면 Redis pub/sub 등으로 바꿔야 한다.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from app.realtime.events import MicState, event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Member:
    participant_id: int
    name: str
    role: str


@dataclass(eq=False)
class Connection:
    websocket: WebSocket
    member: Member
    # pong 과 브로드캐스트가 같은 소켓에 동시에 쓰지 않게 한다
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> bool:
        try:
            async with self.send_lock:
                await self.websocket.send_json(message)
            return True
        except Exception:  # 끊긴 연결은 받는 쪽 루프가 정리한다
            logger.debug("WebSocket 전송 실패", exc_info=True)
            return False


@dataclass
class Room:
    connections: set[Connection] = field(default_factory=set)
    task: asyncio.Task[None] | None = None

    def participant_ids(self) -> set[int]:
        return {c.member.participant_id for c in self.connections}


class ConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}
        self._mic: dict[str, dict[int, MicState]] = {}

    def add(
        self,
        meeting_id: str,
        connection: Connection,
        room_task: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> bool:
        """연결을 넣는다. 그 참가자의 첫 연결이면 True(→ participant.joined).

        room_task 는 회의룸이 새로 생길 때 한 번 띄우는 작업(timer.sync)이고, 룸이 비면 멈춘다.
        """
        room = self._rooms.get(meeting_id)
        if room is None:
            room = self._rooms[meeting_id] = Room()
            if room_task is not None:
                room.task = asyncio.create_task(room_task())
        first = connection.member.participant_id not in room.participant_ids()
        room.connections.add(connection)
        return first

    def remove(self, meeting_id: str, connection: Connection) -> bool:
        """연결을 뺀다. 그 참가자의 마지막 연결이었으면 True(→ participant.left)."""
        room = self._rooms.get(meeting_id)
        if room is None or connection not in room.connections:
            return False
        room.connections.discard(connection)
        last = connection.member.participant_id not in room.participant_ids()
        if not room.connections:
            del self._rooms[meeting_id]
            if room.task is not None:
                room.task.cancel()
        return last

    def count(self, meeting_id: str) -> int:
        room = self._rooms.get(meeting_id)
        return len(room.participant_ids()) if room else 0

    def is_connected(self, meeting_id: str, participant_id: int) -> bool:
        room = self._rooms.get(meeting_id)
        return room is not None and participant_id in room.participant_ids()

    def mic_state(self, meeting_id: str, participant_id: int) -> MicState:
        return self._mic.get(meeting_id, {}).get(participant_id, MicState.ON)

    def set_mic_state(self, meeting_id: str, participant_id: int, state: MicState) -> None:
        self._mic.setdefault(meeting_id, {})[participant_id] = state

    async def broadcast(self, meeting_id: str, type_: str, data: Any = None) -> None:
        """같은 회의의 모든 연결에 { type, data, sentAt } 를 보낸다."""
        room = self._rooms.get(meeting_id)
        if room is None:
            return
        message = event(type_, data)
        await asyncio.gather(*(c.send(message) for c in list(room.connections)))

    async def reset(self) -> None:
        """테스트용: 모든 룸 작업을 멈추고 비운다."""
        for room in self._rooms.values():
            if room.task is not None:
                room.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await room.task
        self._rooms.clear()
        self._mic.clear()


manager = ConnectionManager()
