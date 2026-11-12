"""여러 API와 WebSocket 이벤트가 함께 쓰는 타이머·판정·상태 전이 (API 명세서 > TimerSnapshot).

- 녹음 타임라인 = 일시정지를 뺀 회의 경과 시간. 자막 offsetMs, 안건 recording_*_ms, 합본 위치가 모두 이 값이다
- totalElapsedSeconds = now − startedAt − pausedTotalSeconds − (pausedAt 이 있으면 now − pausedAt)
- 회의가 끝나면 now 대신 endedAt 으로 멈춘다
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError, ErrorCode
from app.api.schemas import CamelModel, KstDatetime
from app.db.models import Agenda, AgendaStatus, Meeting, MeetingStatus

ON_TIME_TOLERANCE_SECONDS = 60


def utcnow() -> datetime:
    return datetime.now(UTC)


class Outcome(StrEnum):
    SAVED = "SAVED"
    ON_TIME = "ON_TIME"
    OVERTIME = "OVERTIME"
    UNKNOWN = "UNKNOWN"


def judge(diff_seconds: int | None) -> Outcome:
    """diffSeconds = actual − planned. −60초 미만 SAVED, −60~+60초 ON_TIME, +60초 초과 OVERTIME."""
    if diff_seconds is None:
        return Outcome.UNKNOWN
    if diff_seconds < -ON_TIME_TOLERANCE_SECONDS:
        return Outcome.SAVED
    if diff_seconds > ON_TIME_TOLERANCE_SECONDS:
        return Outcome.OVERTIME
    return Outcome.ON_TIME


def timeline_ms(meeting: Meeting, now: datetime) -> int:
    """지금의 녹음 위치(ms)."""
    end = meeting.ended_at if meeting.ended_at is not None else now
    elapsed = end - meeting.started_at
    if meeting.paused_at is not None:
        elapsed -= end - meeting.paused_at
    ms = int(elapsed.total_seconds() * 1000) - meeting.paused_total_seconds * 1000
    return max(ms, 0)


class TimerSnapshot(CamelModel):
    server_now: KstDatetime
    status: MeetingStatus
    planned_total_seconds: int
    total_elapsed_seconds: int
    paused_total_seconds: int
    current_agenda_id: int | None
    agenda_allocated_seconds: int
    agenda_elapsed_seconds: int


def build_snapshot(meeting: Meeting, current: Agenda | None, now: datetime) -> TimerSnapshot:
    position = timeline_ms(meeting, now)
    if current is not None:
        allocated = current.allocated_minutes * 60
        elapsed = max(position - (current.recording_start_ms or 0), 0) // 1000
    else:
        allocated = elapsed = 0  # 진행 중 안건이 없으면(종료 후) 0
    return TimerSnapshot(
        server_now=now,
        status=MeetingStatus(meeting.status),
        planned_total_seconds=meeting.planned_total_minutes * 60,
        total_elapsed_seconds=position // 1000,
        paused_total_seconds=meeting.paused_total_seconds,
        current_agenda_id=current.id if current is not None else None,
        agenda_allocated_seconds=allocated,
        agenda_elapsed_seconds=elapsed,
    )


# 회의 상태: IN_PROGRESS ⇄ PAUSED → ENDED
_ALLOWED = {
    MeetingStatus.IN_PROGRESS: {MeetingStatus.PAUSED, MeetingStatus.ENDED},
    MeetingStatus.PAUSED: {MeetingStatus.IN_PROGRESS, MeetingStatus.ENDED},
    MeetingStatus.ENDED: set(),
}


def ensure_transition(current: str, target: MeetingStatus) -> None:
    """끝난 회의는 410 MEETING_ENDED, 그 밖의 맞지 않는 전이는 409 INVALID_STATE."""
    status = MeetingStatus(current)
    if status is MeetingStatus.ENDED:
        raise ApiError(ErrorCode.MEETING_ENDED)
    if target not in _ALLOWED[status]:
        raise ApiError(ErrorCode.INVALID_STATE)


async def lock_meeting(session: AsyncSession, meeting_id: str) -> Meeting:
    """상태를 바꾸는 API(일시 정지·재개·안건 전환·종료)는 이 행 잠금 안에서 처리해 동시 요청을 차례로 받는다."""
    meeting = await session.scalar(select(Meeting).where(Meeting.id == meeting_id).with_for_update())
    if meeting is None:
        raise ApiError(ErrorCode.MEETING_NOT_FOUND)
    return meeting


async def current_agenda(session: AsyncSession, meeting_id: str) -> Agenda | None:
    """진행 중 안건. meetings 에 따로 두지 않고 agendas.status 로 찾는다."""
    return await session.scalar(
        select(Agenda).where(Agenda.meeting_id == meeting_id, Agenda.status == AgendaStatus.IN_PROGRESS.value)
    )
