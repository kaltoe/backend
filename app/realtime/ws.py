"""회의룸 WebSocket 뼈대.

이벤트 형식은 API 명세가 확정되면 맞춘다. 지금은 연결·해제를 알리고, 받은 메시지를 같은 회의에 그대로 전달한다.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.realtime.manager import manager

router = APIRouter(tags=["realtime"])


@router.websocket("/ws/meetings/{meeting_id}")
async def meeting_room(websocket: WebSocket, meeting_id: str) -> None:
    await manager.connect(meeting_id, websocket)
    await manager.broadcast(meeting_id, {"type": "participant_count", "count": manager.count(meeting_id)})
    try:
        while True:
            message = await websocket.receive_json()
            await manager.broadcast(meeting_id, message)
    except WebSocketDisconnect:
        manager.disconnect(meeting_id, websocket)
        await manager.broadcast(meeting_id, {"type": "participant_count", "count": manager.count(meeting_id)})
