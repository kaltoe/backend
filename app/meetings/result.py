"""API-10 회의 결과 조회. GET /api/v1/meetings/{meetingId}/result (참가자)

결과 화면 진입. DB 값만 읽어 즉시 응답한다(요약 문장·녹음 주소는 API-11·13).
- 끝낸 안건(DONE)만. 끝내지 못한 PENDING 안건은 빠진다
- 판정은 ±60초(timer.judge). 회의 소요 시간을 계산하지 못하면(UNKNOWN) 회의·안건 시간 값이 모두 null 이고
  outcome 은 UNKNOWN
- offtopicCount: 그 안건에서 보낸 사담 경고 수([관련 있는 얘기예요]로 닫힌 경고는 빼고 센다)
- 끝나지 않은 회의는 409 INVALID_STATE(2026-11 결정)
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.auth import CurrentCaller
from app.api.errors import ApiError, ErrorCode
from app.api.schemas import CamelModel, Envelope, KstDatetime, ok
from app.db.models import (
    Agenda,
    AgendaStatus,
    EndedBy,
    Meeting,
    MeetingStatus,
    OfftopicEvent,
    Participant,
    PostprocessStatus,
    RecordingStatus,
    SummaryStatus,
)
from app.db.session import get_db
from app.meetings.timer import Outcome, judge

router = APIRouter()


class ResultTime(CamelModel):
    planned_total_seconds: int
    actual_duration_seconds: int | None
    paused_total_seconds: int | None
    diff_seconds: int | None
    outcome: Outcome


class ResultAgenda(CamelModel):
    agenda_id: int
    order_index: int
    title: str
    allocated_seconds: int
    actual_seconds: int | None
    diff_seconds: int | None
    outcome: Outcome
    offtopic_count: int
    recording_start_ms: int | None
    recording_end_ms: int | None


class MeetingResult(CamelModel):
    meeting_id: str
    title: str
    participant_count: int
    started_at: KstDatetime
    ended_at: KstDatetime
    ended_by: EndedBy
    time: ResultTime
    agendas: list[ResultAgenda]
    summary_status: SummaryStatus
    recording_status: RecordingStatus
    postprocess_status: PostprocessStatus


@router.get(
    "/meetings/{meeting_id}/result",
    summary="API-10 회의 결과 조회",
    description=(
        "결과 화면 진입. DB 값만 읽어 즉시 응답합니다(요약 문장·녹음 주소는 없음). 끝내지 못한 안건은 빠집니다. "
        "판정은 ±60초 허용이고, 소요 시간을 계산하지 못하면(`UNKNOWN`) 시간 값이 모두 null 입니다. "
        "`offtopicCount`는 그 안건에서 보낸 사담 경고 수입니다([관련 있는 얘기예요]로 닫힌 경고는 세지 않습니다). "
        "끝나지 않은 회의는 409입니다."
    ),
    responses=docs.success(docs.RESULT_EXAMPLE) | docs.errors(ErrorCode.UNAUTHORIZED, ErrorCode.INVALID_STATE),
)
async def get_result(
    meeting_id: str, caller: CurrentCaller, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[MeetingResult]:
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None:  # 토큰은 이 회의 것이므로 없을 일이 거의 없다(API-04 와 같이 401)
        raise ApiError(ErrorCode.UNAUTHORIZED)
    if meeting.status != MeetingStatus.ENDED or meeting.ended_at is None or meeting.ended_by is None:
        raise ApiError(ErrorCode.INVALID_STATE)
    return ok(await build_result(session, meeting))


async def build_result(session: AsyncSession, meeting: Meeting) -> MeetingResult:
    """API-10·16 공통. 끝난 회의(ended_at·ended_by 가 있는)만 넘긴다. 대기 중에는 끝낼 수 없어 started_at 도 있다."""
    assert meeting.started_at is not None and meeting.ended_at is not None and meeting.ended_by is not None
    meeting_id = meeting.id
    participant_count = await session.scalar(
        select(func.count()).select_from(Participant).where(Participant.meeting_id == meeting_id)
    )
    counts = dict(
        (
            await session.execute(
                select(OfftopicEvent.agenda_id, func.count())
                .where(OfftopicEvent.meeting_id == meeting_id, OfftopicEvent.dismissed_at.is_(None))
                .group_by(OfftopicEvent.agenda_id)
            )
        ).all()
    )
    agendas = await session.scalars(
        select(Agenda)
        .where(Agenda.meeting_id == meeting_id, Agenda.status == AgendaStatus.DONE.value)
        .order_by(Agenda.order_index)
    )

    planned = meeting.planned_total_minutes * 60
    actual = meeting.actual_duration_seconds
    known = actual is not None  # UNKNOWN 이면 회의·안건 시간 값을 모두 비운다

    def agenda_out(a: Agenda) -> ResultAgenda:
        allocated = a.allocated_minutes * 60
        spent = (
            (a.recording_end_ms - a.recording_start_ms) // 1000
            if known and a.recording_start_ms is not None and a.recording_end_ms is not None
            else None
        )
        diff = spent - allocated if spent is not None else None
        return ResultAgenda(
            agenda_id=a.id,
            order_index=a.order_index,
            title=a.title,
            allocated_seconds=allocated,
            actual_seconds=spent,
            diff_seconds=diff,
            outcome=judge(diff),
            offtopic_count=counts.get(a.id, 0),
            recording_start_ms=a.recording_start_ms,
            recording_end_ms=a.recording_end_ms,
        )

    diff = actual - planned if actual is not None else None
    return MeetingResult(
        meeting_id=meeting.id,
        title=meeting.title,
        participant_count=participant_count or 0,
        started_at=meeting.started_at,
        ended_at=meeting.ended_at,
        ended_by=EndedBy(meeting.ended_by),
        time=ResultTime(
            planned_total_seconds=planned,
            actual_duration_seconds=actual,
            paused_total_seconds=meeting.paused_total_seconds if known else None,
            diff_seconds=diff,
            outcome=judge(diff),
        ),
        agendas=[agenda_out(a) for a in agendas],
        summary_status=SummaryStatus(meeting.summary_status or SummaryStatus.PROCESSING),
        recording_status=RecordingStatus(meeting.recording_status or RecordingStatus.PROCESSING),
        postprocess_status=PostprocessStatus(meeting.postprocess_status),
    )
