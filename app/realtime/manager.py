"""회의룸별 WebSocket 연결을 관리하고 같은 회의의 모든 참여자에게 이벤트를 보낸다.

서버 한 대 기준의 메모리 구현이다. 서버를 여러 대로 늘리면 Redis pub/sub 등으로 바꿔야 한다.
"""

from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        # 회의별 연결 → 그 연결의 참가자 ID(토큰 확인 전 뼈대 연결은 None)
        self._rooms: dict[str, dict[WebSocket, int | None]] = defaultdict(dict)

    async def connect(self, meeting_id: str, websocket: WebSocket, participant_id: int | None = None) -> None:
        await websocket.accept()
        self._rooms[meeting_id][websocket] = participant_id

    def disconnect(self, meeting_id: str, websocket: WebSocket) -> None:
        room = self._rooms.get(meeting_id)
        if room is None:
            return
        room.pop(websocket, None)
        if not room:
            del self._rooms[meeting_id]

    def count(self, meeting_id: str) -> int:
        """API-01 connectedCount."""
        return len(self._rooms.get(meeting_id, ()))

    def is_connected(self, meeting_id: str, participant_id: int) -> bool:
        """API-03 이어받기 판단: 그 참가자의 소켓이 지금 붙어 있는가."""
        return participant_id in self._rooms.get(meeting_id, {}).values()

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
