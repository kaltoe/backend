"""API-05 일시 정지, API-06 재개. POST /api/v1/meetings/{meetingId}/pause · /resume (진행자 전용)

일시 정지 중에는 녹음 타임라인이 흐르지 않는다. 그래야 자막 시각, 안건 구간, 플레이어 위치가 맞는다.
회의 행을 잠근 채 바꾸고, 커밋한 뒤 전원에게 meeting.paused / meeting.resumed 를 보낸다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CurrentHost
from app.api.schemas import Envelope, ok
from app.db.models import MeetingStatus
from app.db.session import get_db
from app.meetings import timer
from app.realtime.events import MeetingPaused, MeetingResumed
from app.realtime.manager import manager

router = APIRouter()


@router.post("/meetings/{meeting_id}/pause")
async def pause_meeting(
    meeting_id: str, host: CurrentHost, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[MeetingPaused]:
    meeting = await timer.lock_meeting(session, meeting_id)
    timer.ensure_transition(meeting.status, MeetingStatus.PAUSED)
    now = timer.utcnow()
    timer.pause(meeting, now)
    snapshot = timer.build_snapshot(meeting, await timer.current_agenda(session, meeting_id), now)
    await session.commit()

    # ponytail: 정지 알림 지점. #20 은 PAUSED 동안 음성을 버리고,
    # #21/#22 는 리턴제로 세션을 EOS 로 닫고, #23 은 사담 판정을 멈추는 호출을 여기에 붙인다
    paused = MeetingPaused(paused_at=now, timer=snapshot)
    await manager.broadcast(meeting_id, "meeting.paused", paused)
    return ok(paused)


@router.post("/meetings/{meeting_id}/resume")
async def resume_meeting(
    meeting_id: str, host: CurrentHost, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[MeetingResumed]:
    meeting = await timer.lock_meeting(session, meeting_id)
    timer.ensure_transition(meeting.status, MeetingStatus.IN_PROGRESS)
    now = timer.utcnow()
    timer.resume(meeting, now)
    snapshot = timer.build_snapshot(meeting, await timer.current_agenda(session, meeting_id), now)
    await session.commit()

    # ponytail: 재개 알림 지점. #21/#22 는 리턴제로 세션을 새로 열고, #23 은 사담 판정을 다시 시작한다
    resumed = MeetingResumed(resumed_at=now, timer=snapshot)
    await manager.broadcast(meeting_id, "meeting.resumed", resumed)
    return ok(resumed)
