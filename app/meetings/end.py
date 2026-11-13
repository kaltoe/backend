"""API-08 회의 종료. POST /api/v1/meetings/{meetingId}/end (진행자 전용)

종료 로직은 finish() 하나로 두고 자동 종료(#19)가 ended_by=AUTO 로 같이 쓴다.
- 일시 정지 중이면 멈춘 시간을 합계에 더한 뒤 지금 안건을 닫는다. 끝내지 못한 PENDING 안건은 그대로(결과에서 빠짐)
- 종료 후 처리 상태: recording·summary = PROCESSING, postprocess = 대면 PROCESSING / 비대면 NONE
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CurrentHost
from app.api.schemas import CamelModel, Envelope, KstDatetime, ok
from app.db.models import (
    AgendaStatus,
    EndedBy,
    Meeting,
    MeetingMode,
    MeetingStatus,
    PostprocessStatus,
    RecordingStatus,
    SummaryStatus,
)
from app.db.session import get_db
from app.meetings import timer
from app.realtime import audio
from app.realtime.events import MeetingEnded
from app.realtime.manager import manager

router = APIRouter()


class EndedMeeting(CamelModel):
    meeting_id: str
    status: MeetingStatus
    ended_at: KstDatetime
    ended_by: EndedBy
    actual_duration_seconds: int | None


def actual_duration_seconds(meeting: Meeting) -> int | None:
    """endedAt − startedAt − pausedTotalSeconds. 계산할 수 없거나 말이 안 되는 값이면 None(결과 UNKNOWN)."""
    if meeting.started_at is None or meeting.ended_at is None or meeting.paused_at is not None:
        return None
    wall = int((meeting.ended_at - meeting.started_at).total_seconds())
    duration = wall - meeting.paused_total_seconds
    if duration <= 0 or duration > wall:
        return None
    return duration


async def finish(session: AsyncSession, meeting: Meeting, ended_by: EndedBy, now: datetime) -> MeetingEnded:
    """잠근 회의를 끝낸다. 끝나지 않은 회의인지는 호출하는 쪽이 먼저 확인한다. 커밋은 호출하는 쪽이 한다."""
    if meeting.status == MeetingStatus.PAUSED:
        timer.resume(meeting, now)  # 멈춘 시간을 합계에 더하고 paused_at 을 비운다
    current = await timer.current_agenda(session, meeting.id)
    if current is not None:
        current.status = AgendaStatus.DONE.value
        current.recording_end_ms = timer.timeline_ms(meeting, now)

    meeting.status = MeetingStatus.ENDED.value
    meeting.ended_at = now
    meeting.ended_by = ended_by.value
    meeting.paused_at = None
    meeting.actual_duration_seconds = actual_duration_seconds(meeting)
    meeting.recording_status = RecordingStatus.PROCESSING.value
    meeting.summary_status = SummaryStatus.PROCESSING.value
    meeting.postprocess_status = (
        PostprocessStatus.PROCESSING.value
        if meeting.meeting_mode == MeetingMode.IN_PERSON
        else PostprocessStatus.NONE.value  # 비대면은 CLOVA 를 쓰지 않는다
    )
    return MeetingEnded(ended_at=now, ended_by=ended_by, actual_duration_seconds=meeting.actual_duration_seconds)


async def announce(meeting_id: str, ended: MeetingEnded) -> None:
    """커밋한 뒤 부른다."""
    await manager.broadcast(meeting_id, "meeting.ended", ended)
    await audio.close_all(meeting_id)  # WS-02 음성 연결은 4410 으로 닫는다. 녹음 파일은 청크마다 닫혀 있다
    # ponytail: 종료 후 처리 지점. #21/#22 는 리턴제로 세션을 닫고,
    # #25 는 합본 만들기를 백그라운드로 시작하는 호출을 여기에 붙인다


@router.post("/meetings/{meeting_id}/end")
async def end_meeting(
    meeting_id: str, host: CurrentHost, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[EndedMeeting]:
    meeting = await timer.lock_meeting(session, meeting_id)
    timer.ensure_transition(meeting.status, MeetingStatus.ENDED)  # 이미 끝난 회의는 410
    ended = await finish(session, meeting, EndedBy.HOST, timer.utcnow())
    await session.commit()
    await announce(meeting_id, ended)
    return ok(
        EndedMeeting(
            meeting_id=meeting_id,
            status=MeetingStatus.ENDED,
            ended_at=ended.ended_at,
            ended_by=ended.ended_by,
            actual_duration_seconds=ended.actual_duration_seconds,
        )
    )
