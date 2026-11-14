"""API-07 다음 안건으로 전환. POST /api/v1/meetings/{meetingId}/agendas/next (진행자 전용)

- fromAgendaId 가 지금 진행 중 안건과 다르면 409 AGENDA_CHANGED. 두 번 누름·재전송·동시 요청에도
  회의 행 잠금 안에서 확인하므로 한 번만 넘어간다
- 지금 안건 recording_end_ms 와 다음 안건 recording_start_ms 는 같은 녹음 위치
- 일시 정지 중이면 자동 재개하고, meeting.resumed 없이 agenda.changed 하나만 보낸다
"""

from typing import Annotated

from fastapi import APIRouter, Body, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
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


@router.post(
    "/meetings/{meeting_id}/agendas/next",
    summary="API-07 다음 안건으로 전환",
    description=(
        "[다음 안건으로] 확인. 진행자만 호출할 수 있습니다. `fromAgendaId`는 지금 진행 중인 안건이어야 하고, "
        "다르면 409 `AGENDA_CHANGED`입니다(API-04로 화면만 다시 맞춤). 일시 정지 중이면 먼저 재개한 뒤 넘깁니다. "
        "지금 안건을 끝내고 다음 안건을 시작하며, 전원에게 WS-01 `agenda.changed`를 보냅니다."
    ),
    responses=docs.success(docs.AGENDA_CHANGED)
    | docs.errors(
        ErrorCode.VALIDATION_ERROR,
        ErrorCode.UNAUTHORIZED,
        ErrorCode.HOST_ONLY,
        ErrorCode.LAST_AGENDA,
        ErrorCode.AGENDA_CHANGED,
        ErrorCode.MEETING_ENDED,
        field="fromAgendaId",
    ),
)
async def next_agenda(
    meeting_id: str,
    body: Annotated[NextAgendaRequest, Body(openapi_examples=docs.NEXT_REQUEST)],
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

    # ponytail: was_paused 면 #23 이 사담 판정을 재개하는 호출을 여기에 붙인다(리턴제로 세션은 첫 소리에 열린다)
    await manager.broadcast(meeting_id, "agenda.changed", changed)
    return ok(changed)
