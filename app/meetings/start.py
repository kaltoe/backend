"""API-19 회의 시작. POST /api/v1/meetings/{meetingId}/start (진행자 전용)

API-02 로 만든 회의는 대기(WAITING)다. 진행자가 대기 화면에서 [회의 시작]을 누르면 이 시각부터 타이머가 흐른다.
started_at = now, 첫 안건 IN_PROGRESS(recording_start_ms = 0). 대기 시간은 경과·녹음 위치에 들어가지 않는다.
회의 행을 잠근 채 바꿔, 여러 기기에서 동시에 눌러도 한 번만 시작된다(두 번째는 409).
커밋한 뒤 전원에게 WS-01 meeting.started 를 보낸다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.auth import CurrentHost
from app.api.errors import ApiError, ErrorCode
from app.api.schemas import Envelope, ok
from app.db.models import AgendaStatus, MeetingStatus
from app.db.session import get_db
from app.meetings import timer
from app.realtime.events import MeetingStarted
from app.realtime.manager import manager

router = APIRouter()


@router.post(
    "/meetings/{meeting_id}/start",
    summary="API-19 회의 시작",
    description=(
        "대기 화면의 [회의 시작]. 진행자만, 대기(`WAITING`)일 때만 호출할 수 있습니다. "
        "지금을 `startedAt`으로 두고 첫 안건을 진행 중으로 바꿔 타이머를 시작합니다. "
        "전원에게 WS-01 `meeting.started`를 보내고, 참가자 화면은 이 이벤트로 대기 팝업을 닫습니다. "
        "이미 시작된 회의면 409이고, 다른 기기에서 먼저 누른 경우이므로 프론트는 에러 없이 API-04로 화면을 맞춥니다."
    ),
    responses=docs.success(docs.STARTED)
    | docs.errors(ErrorCode.UNAUTHORIZED, ErrorCode.HOST_ONLY, ErrorCode.INVALID_STATE, ErrorCode.MEETING_ENDED),
)
async def start_meeting(
    meeting_id: str, host: CurrentHost, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[MeetingStarted]:
    meeting = await timer.lock_meeting(session, meeting_id)
    timer.ensure_transition(meeting.status, MeetingStatus.IN_PROGRESS)
    if meeting.status != MeetingStatus.WAITING:  # 일시 정지에서 IN_PROGRESS 로 가는 건 재개(API-06)
        raise ApiError(ErrorCode.INVALID_STATE)
    first = await timer.snapshot_agenda(session, meeting)
    assert first is not None  # 안건은 1개 이상(API-02 검증)
    now = timer.utcnow()
    meeting.status, meeting.started_at = MeetingStatus.IN_PROGRESS.value, now
    first.status, first.recording_start_ms = AgendaStatus.IN_PROGRESS.value, 0
    started = MeetingStarted(started_at=now, timer=timer.build_snapshot(meeting, first, now))
    await session.commit()

    await manager.broadcast(meeting_id, "meeting.started", started)
    return ok(started)
