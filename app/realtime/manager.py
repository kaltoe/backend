"""회의룸별 WebSocket 연결을 관리하고 같은 회의의 모든 참여자에게 이벤트를 보낸다.

서버 한 대 기준의 메모리 구현이다. 서버를 여러 대로 늘리면 Redis pub/sub 등으로 바꿔야 한다.
"""

from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, meeting_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._rooms[meeting_id].add(websocket)

    def disconnect(self, meeting_id: str, websocket: WebSocket) -> None:
        room = self._rooms.get(meeting_id)
        if room is None:
            return
        room.discard(websocket)
        if not room:
            del self._rooms[meeting_id]

    def count(self, meeting_id: str) -> int:
        return len(self._rooms.get(meeting_id, ()))

    async def broadcast(self, meeting_id: str, event: dict[str, Any]) -> None:
        dead = []
        for websocket in list(self._rooms.get(meeting_id, ())):
            try:
                await websocket.send_json(event)
            except Exception:  # 끊긴 연결은 정리한다
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(meeting_id, websocket)


manager = ConnectionManager()
