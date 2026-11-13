"""API-05 일시 정지, API-06 재개. POST /api/v1/meetings/{meetingId}/pause · /resume (진행자 전용)

일시 정지 중에는 녹음 타임라인이 흐르지 않는다. 그래야 자막 시각, 안건 구간, 플레이어 위치가 맞는다.
회의 행을 잠근 채 바꾸고, 커밋한 뒤 전원에게 meeting.paused / meeting.resumed 를 보낸다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.auth import CurrentHost
from app.api.errors import ErrorCode
from app.api.schemas import Envelope, ok
from app.db.models import MeetingStatus
from app.db.session import get_db
from app.meetings import timer
from app.realtime.events import MeetingPaused, MeetingResumed
from app.realtime.manager import manager
from app.stt import live

router = APIRouter()


@router.post(
    "/meetings/{meeting_id}/pause",
    summary="API-05 일시 정지",
    description=(
        "진행자만, 진행 중일 때만 호출할 수 있습니다. 타이머를 멈추고, 그 뒤 들어오는 음성은 버리며 "
        "실시간 자막 세션을 닫습니다. 전원에게 WS-01 `meeting.paused`를 보냅니다."
    ),
    responses=docs.success(docs.PAUSED)
    | docs.errors(ErrorCode.UNAUTHORIZED, ErrorCode.HOST_ONLY, ErrorCode.INVALID_STATE, ErrorCode.MEETING_ENDED),
)
async def pause_meeting(
    meeting_id: str, host: CurrentHost, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[MeetingPaused]:
    meeting = await timer.lock_meeting(session, meeting_id)
    timer.ensure_transition(meeting.status, MeetingStatus.PAUSED)
    now = timer.utcnow()
    timer.pause(meeting, now)
    snapshot = timer.build_snapshot(meeting, await timer.current_agenda(session, meeting_id), now)
    await session.commit()

    await live.pause(meeting_id)  # 리턴제로 세션을 EOS 로 닫는다. 재개 후 첫 소리에 새 세션을 연다
    # ponytail: 정지 알림 지점. #23 은 사담 판정을 멈추는 호출을 여기에 붙인다
    paused = MeetingPaused(paused_at=now, timer=snapshot)
    await manager.broadcast(meeting_id, "meeting.paused", paused)
    return ok(paused)


@router.post(
    "/meetings/{meeting_id}/resume",
    summary="API-06 회의 재개",
    description=(
        "진행자만, 일시 정지 중일 때만 호출할 수 있습니다. 멈춘 시간은 `pausedTotalSeconds`에만 더하고 "
        "타이머를 다시 돌립니다. 실시간 자막 세션은 재개 후 첫 소리에 새로 엽니다. "
        "전원에게 WS-01 `meeting.resumed`를 보냅니다."
    ),
    responses=docs.success(docs.RESUMED)
    | docs.errors(ErrorCode.UNAUTHORIZED, ErrorCode.HOST_ONLY, ErrorCode.INVALID_STATE, ErrorCode.MEETING_ENDED),
)
async def resume_meeting(
    meeting_id: str, host: CurrentHost, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[MeetingResumed]:
    meeting = await timer.lock_meeting(session, meeting_id)
    timer.ensure_transition(meeting.status, MeetingStatus.IN_PROGRESS)
    now = timer.utcnow()
    timer.resume(meeting, now)
    snapshot = timer.build_snapshot(meeting, await timer.current_agenda(session, meeting_id), now)
    await session.commit()

    # ponytail: 재개 알림 지점. 리턴제로 세션은 재개 후 첫 소리에 열린다. #23 은 사담 판정을 다시 시작한다
    resumed = MeetingResumed(resumed_at=now, timer=snapshot)
    await manager.broadcast(meeting_id, "meeting.resumed", resumed)
    return ok(resumed)
