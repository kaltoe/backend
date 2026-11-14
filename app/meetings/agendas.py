"""API-07 다음 안건으로 전환. POST /api/v1/meetings/{meetingId}/agendas/next (진행자 전용)

- fromAgendaId 가 지금 진행 중 안건과 다르면 409 AGENDA_CHANGED. 두 번 누름·재전송·동시 요청에도
  회의 행 잠금 안에서 확인하므로 한 번만 넘어간다
- 지금 안건 recording_end_ms 와 다음 안건 recording_start_ms 는 같은 녹음 위치
- 일시 정지 중이면 자동 재개하고, meeting.resumed 없이 agenda.changed 하나만 보낸다
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CurrentHost
from app.api.errors import ApiError, ErrorCode
from app.api.schemas import CamelModel, Envelope, ok
from app.db.models import Agenda, AgendaStatus, MeetingStatus
from app.db.session import get_db
from app.meetings import timer
from app.meetings.schemas import StrictInt
from app.meetings.timer import Outcome, TimerSnapshot
from app.realtime.manager import manager

router = APIRouter()


class NextAgendaRequest(CamelModel):
    from_agenda_id: StrictInt


class PreviousAgenda(CamelModel):
    agenda_id: int
    actual_seconds: int
    diff_seconds: int
    outcome: Outcome


class CurrentAgenda(CamelModel):
    agenda_id: int
    order_index: int
    title: str
    allocated_seconds: int


class AgendaChanged(CamelModel):
    previous_agenda: PreviousAgenda
    current_agenda: CurrentAgenda
    timer: TimerSnapshot


@router.post("/meetings/{meeting_id}/agendas/next")
async def next_agenda(
    meeting_id: str,
    body: NextAgendaRequest,
    host: CurrentHost,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Envelope[AgendaChanged]:
    meeting = await timer.lock_meeting(session, meeting_id)
    if meeting.status == MeetingStatus.ENDED:
        raise ApiError(ErrorCode.MEETING_ENDED)
    current = await timer.current_agenda(session, meeting_id)
    # 다른 회의의 안건 ID 도 진행 중 안건과 다르므로 같은 409 다
    if current is None or current.id != body.from_agenda_id:
        raise ApiError(ErrorCode.AGENDA_CHANGED)
    following = await session.scalar(
        select(Agenda)
        .where(Agenda.meeting_id == meeting_id, Agenda.order_index > current.order_index)
        .order_by(Agenda.order_index)
        .limit(1)
    )
    if following is None:
        raise ApiError(ErrorCode.LAST_AGENDA)

    now = timer.utcnow()
    was_paused = meeting.status == MeetingStatus.PAUSED
    if was_paused:
        timer.resume(meeting, now)
    position = timer.timeline_ms(meeting, now)

    current.status = AgendaStatus.DONE.value
    current.recording_end_ms = position
    # 진행 중 안건은 회의마다 하나(부분 유니크 인덱스)라, 지금 안건을 먼저 DONE 으로 내보낸다
    await session.flush()
    following.status = AgendaStatus.IN_PROGRESS.value
    following.recording_start_ms = position

    actual = (position - (current.recording_start_ms or 0)) // 1000
    diff = actual - current.allocated_minutes * 60
    changed = AgendaChanged(
        previous_agenda=PreviousAgenda(
            agenda_id=current.id, actual_seconds=actual, diff_seconds=diff, outcome=timer.judge(diff)
        ),
        current_agenda=CurrentAgenda(
            agenda_id=following.id,
            order_index=following.order_index,
            title=following.title,
            allocated_seconds=following.allocated_minutes * 60,
        ),
        timer=timer.build_snapshot(meeting, following, now),
    )
    await session.commit()

    # ponytail: was_paused 면 #21/#22 가 닫아 둔 리턴제로 세션을 다시 열고,
    # #23 이 사담 판정을 재개하는 호출을 여기에 붙인다
    await manager.broadcast(meeting_id, "agenda.changed", changed)
    return ok(changed)
