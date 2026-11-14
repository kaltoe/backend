"""WS-01 상태 동기화. wss://…/ws/meetings/{meetingId}?token={accessToken}

명령은 REST 로 받고 결과를 이 소켓으로 퍼뜨린다. 클라이언트가 보내는 메시지는 두 가지다.
- { "type": "ping" } → 보낸 쪽에만 pong
- { "type": "mic.status", "data": { "state": "ON" | "OFF" | "DENIED" } } → 전원에게 participant.updated
60초 동안 아무 메시지도 없으면 연결을 끊고 participant.left 를 보낸다.
"""

import asyncio
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.api.auth import read_token
from app.db.models import MeetingStatus, Participant, Role
from app.db.session import SessionFactory, get_session_factory
from app.meetings.entry import find_meeting
from app.meetings.timer import build_snapshot, current_agenda, utcnow
from app.realtime.events import (
    CLOSE_ENDED,
    CLOSE_HEARTBEAT_TIMEOUT,
    CLOSE_NOT_FOUND,
    CLOSE_UNAUTHORIZED,
    ErrorCode,
    MicState,
    ParticipantJoined,
    ParticipantLeft,
    ParticipantUpdated,
    TimerSync,
    error_event,
    event,
)
from app.realtime.manager import Connection, Member, manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])

HEARTBEAT_TIMEOUT_SECONDS = 60.0  # 클라이언트는 25초마다 ping
TIMER_SYNC_SECONDS = 30.0


async def authenticate(sessions: SessionFactory, meeting_id: str, token: str | None) -> Member | int:
    """연결해도 되는 참가자면 Member, 아니면 close code."""
    async with sessions() as session:
        meeting = await find_meeting(session, meeting_id)
        if meeting is None:
            return CLOSE_NOT_FOUND
        caller = read_token(token, meeting_id) if token else None
        participant = await session.get(Participant, caller.participant_id) if caller else None
        if participant is None or participant.meeting_id != meeting_id:
            return CLOSE_UNAUTHORIZED
        # 만료(생성 14일 후)도 API-01·03 의 410 처럼 더는 들어올 수 없는 회의로 본다
        if meeting.status == MeetingStatus.ENDED or meeting.expires_at <= utcnow():
            return CLOSE_ENDED
        return Member(participant_id=participant.id, name=participant.name, role=participant.role)


async def timer_sync_loop(sessions: SessionFactory, meeting_id: str) -> None:
    """진행 중일 때만 30초마다 timer.sync. 회의가 끝나면 멈춘다."""
    while True:
        await asyncio.sleep(TIMER_SYNC_SECONDS)
        try:
            async with sessions() as session:
                meeting = await find_meeting(session, meeting_id)
                if meeting is None or meeting.status == MeetingStatus.ENDED:
                    return
                if meeting.status != MeetingStatus.IN_PROGRESS:
                    continue
                snapshot = build_snapshot(meeting, await current_agenda(session, meeting_id), utcnow())
            await manager.broadcast(meeting_id, "timer.sync", TimerSync(timer=snapshot))
        except Exception:
            logger.exception("timer.sync 실패: %s", meeting_id)


async def handle_message(meeting_id: str, connection: Connection, raw: str | None) -> None:
    try:
        message: Any = json.loads(raw) if raw is not None else None
    except ValueError:
        message = None
    kind = message.get("type") if isinstance(message, dict) else None

    if kind == "ping":
        await connection.send(event("pong"))
        return
    if kind == "mic.status":
        data = message.get("data")
        state = data.get("state") if isinstance(data, dict) else None
        if isinstance(state, str) and state in MicState.__members__:
            participant_id = connection.member.participant_id
            manager.set_mic_state(meeting_id, participant_id, MicState(state))
            await manager.broadcast(
                meeting_id,
                "participant.updated",
                ParticipantUpdated(participant_id=participant_id, mic_state=MicState(state)),
            )
            return
    await connection.send(error_event(ErrorCode.INVALID_MESSAGE))


async def receive_text(websocket: WebSocket) -> str | None:
    """텍스트 메시지를 받는다. 바이너리면 None. 끊기면 WebSocketDisconnect."""
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    text: str | None = message.get("text")
    return text


@router.websocket("/ws/meetings/{meeting_id}")
async def status_channel(
    websocket: WebSocket,
    meeting_id: str,
    sessions: Annotated[SessionFactory, Depends(get_session_factory)],
    token: str | None = None,
) -> None:
    # close code 를 브라우저에 전하려면 먼저 연결을 받아야 한다(받기 전에 닫으면 HTTP 403)
    await websocket.accept()
    result = await authenticate(sessions, meeting_id, token)
    if isinstance(result, int):
        await websocket.close(code=result)
        return

    connection = Connection(websocket=websocket, member=result, reader=asyncio.current_task())
    first = manager.add(meeting_id, connection, room_task=lambda: timer_sync_loop(sessions, meeting_id))
    # 목록에 넣은 뒤의 모든 await 는 try 안에 둔다. 막힌 전송으로 이 태스크가 취소돼도 finally 가 목록에서 뺀다
    try:
        if first:
            await manager.broadcast(
                meeting_id,
                "participant.joined",
                ParticipantJoined(
                    participant_id=result.participant_id,
                    name=result.name,
                    role=Role(result.role),
                    connected_count=manager.count(meeting_id),
                ),
            )
        while True:
            try:
                raw = await asyncio.wait_for(receive_text(websocket), timeout=HEARTBEAT_TIMEOUT_SECONDS)
            except TimeoutError:
                await websocket.close(code=CLOSE_HEARTBEAT_TIMEOUT)
                break
            try:
                await handle_message(meeting_id, connection, raw)
            except Exception:
                logger.exception("WebSocket 메시지 처리 실패: %s", meeting_id)
                await connection.send(error_event(ErrorCode.INTERNAL_ERROR))
    except WebSocketDisconnect:
        pass
    finally:
        if manager.remove(meeting_id, connection):
            await manager.broadcast(
                meeting_id,
                "participant.left",
                ParticipantLeft(participant_id=result.participant_id, connected_count=manager.count(meeting_id)),
            )
