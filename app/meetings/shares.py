"""공유 뷰(#32). 링크를 받은 사람이 토큰 없이 shareId 로 여는 읽기 전용 화면.

- API-16 GET /api/v1/shares/{shareId}              회의록(결과 + 안건별 3줄 요약)
- API-17 GET /api/v1/shares/{shareId}/transcripts  전체 기록. API-14 AUTO 와 같고 utteranceId 만 없다
- API-18 GET /api/v1/shares/{shareId}/recording    합본 녹음. API-13 과 같은 응답·같은 파일

링크가 없으면 404 SHARE_NOT_FOUND, 만료(회의 종료 + 7일, API-15)면 410 SHARE_EXPIRED. 사용자에게 보이는 문구는 같다.
녹음 보관 기간(종료 + 14일)이 지나면 API-18 만 410 RECORDING_EXPIRED. 링크가 7일이라 지금은 링크가 먼저 끝나지만,
결제로 링크를 다시 열 때를 생각해 녹음 쪽 검사도 둔다.
결과·전체 기록·녹음은 API-10(result.py)·API-14(transcripts.py)·API-13(recording.py)의 함수를 그대로 쓴다.
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.errors import ApiError, ErrorCode
from app.api.schemas import CamelModel, Envelope, KstDatetime, ok
from app.db.models import Agenda, Meeting, MeetingStatus, ShareLink, SummaryStatus, UtteranceSource
from app.db.session import get_db
from app.meetings import timer
from app.meetings.recording import RecordingOut, recording_of
from app.meetings.recording import expired as recording_expired
from app.meetings.result import ResultTime, build_result
from app.meetings.timer import Outcome
from app.meetings.transcripts import group_transcripts

router = APIRouter()


class SharedAgenda(CamelModel):
    agenda_id: int
    order_index: int
    title: str
    allocated_seconds: int
    actual_seconds: int | None
    diff_seconds: int | None
    outcome: Outcome
    offtopic_count: int
    summary_lines: list[str]  # 요약이 DONE 이 아니거나 발언이 없던 안건이면 []. 항상 배열


class SharedMeeting(CamelModel):
    title: str
    participant_count: int
    started_at: KstDatetime
    ended_at: KstDatetime
    time: ResultTime
    agendas: list[SharedAgenda]
    summary_status: SummaryStatus
    expires_at: KstDatetime


class SharedItem(CamelModel):
    speaker_label: str | None
    text: str
    offset_ms: int


class SharedGroup(CamelModel):
    agenda_id: int
    order_index: int
    title: str
    items: list[SharedItem]


class SharedTranscripts(CamelModel):
    source: UtteranceSource
    groups: list[SharedGroup]


async def open_link(session: AsyncSession, share_id: str) -> tuple[ShareLink, Meeting]:
    """링크와 회의. 없으면 404, 만료면 410."""
    link = await session.scalar(select(ShareLink).where(ShareLink.share_id == share_id))
    meeting = await session.get(Meeting, link.meeting_id) if link is not None else None
    # 링크는 끝난 회의에서만 만들어지므로(API-15) 회의가 없거나 끝나지 않았으면 잘못된 링크로 본다
    if link is None or meeting is None or meeting.status != MeetingStatus.ENDED or meeting.ended_by is None:
        raise ApiError(ErrorCode.SHARE_NOT_FOUND)
    if timer.utcnow() >= link.expires_at:
        raise ApiError(ErrorCode.SHARE_EXPIRED)
    return link, meeting


SHARE_ERRORS = (ErrorCode.SHARE_NOT_FOUND, ErrorCode.SHARE_EXPIRED)


@router.get(
    "/shares/{share_id}",
    summary="API-16 공유 회의록 조회",
    description=(
        "공유 뷰 진입. 토큰 없이 엽니다. 결과(API-10)와 안건별 3줄 요약을 한 번에 줍니다. "
        "`summaryLines`는 항상 배열이고, 요약이 `DONE`이 아니거나 발언이 없던 안건이면 `[]`입니다. "
        "녹음은 API-18로 받습니다. 잘못된 링크는 404, 만료된 링크는 410이고 보여 줄 문구는 같습니다."
    ),
    responses=docs.success(docs.SHARED_MEETING) | docs.errors(*SHARE_ERRORS),
)
async def get_shared_meeting(
    share_id: str, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[SharedMeeting]:
    link, meeting = await open_link(session, share_id)
    result = await build_result(session, meeting)
    lines: dict[int, list[str]] = {}
    if result.summary_status == SummaryStatus.DONE:
        rows = await session.execute(select(Agenda.id, Agenda.summary_lines).where(Agenda.meeting_id == meeting.id))
        lines = {agenda_id: summary or [] for agenda_id, summary in rows}
    return ok(
        SharedMeeting(
            title=result.title,
            participant_count=result.participant_count,
            started_at=result.started_at,
            ended_at=result.ended_at,
            time=result.time,
            agendas=[
                SharedAgenda(
                    agenda_id=a.agenda_id,
                    order_index=a.order_index,
                    title=a.title,
                    allocated_seconds=a.allocated_seconds,
                    actual_seconds=a.actual_seconds,
                    diff_seconds=a.diff_seconds,
                    outcome=a.outcome,
                    offtopic_count=a.offtopic_count,
                    summary_lines=lines.get(a.agenda_id, []),
                )
                for a in result.agendas
            ],
            summary_status=result.summary_status,
            expires_at=link.expires_at,
        )
    )


@router.get(
    "/shares/{share_id}/transcripts",
    summary="API-17 공유 전체 기록",
    description=(
        "공유 뷰 [전체 기록]. API-14를 `source=AUTO`로 부른 것과 같고 `utteranceId`만 없습니다. "
        "후처리가 끝났으면 `POSTPROCESS`, 아니면 `REALTIME`입니다."
    ),
    responses=docs.success(docs.SHARED_TRANSCRIPTS) | docs.errors(*SHARE_ERRORS),
)
async def get_shared_transcripts(
    share_id: str, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[SharedTranscripts]:
    _, meeting = await open_link(session, share_id)
    full = await group_transcripts(session, meeting)
    return ok(
        SharedTranscripts(
            source=full.source,
            groups=[
                SharedGroup(
                    agenda_id=g.agenda_id,
                    order_index=g.order_index,
                    title=g.title,
                    items=[
                        SharedItem(speaker_label=i.speaker_label, text=i.text, offset_ms=i.offset_ms) for i in g.items
                    ],
                )
                for g in full.groups
            ],
        )
    )


@router.get(
    "/shares/{share_id}/recording",
    summary="API-18 공유 녹음 조회",
    description=(
        "공유 뷰에서 문장을 눌러 재생할 합본 녹음. API-13과 같은 응답·같은 파일입니다. "
        "`READY`면 `url`을 audio src 에 그대로 쓰고, `PROCESSING`이면 3초마다 다시 부릅니다. "
        "재생 중 주소가 만료되거나 403이 나면 다시 불러 주소만 바꿉니다. "
        "녹음 보관 기간(종료 + 14일)이 지나면 링크가 살아 있어도 410 `RECORDING_EXPIRED`이고, "
        "프론트는 플레이어만 숨깁니다."
    ),
    responses=docs.success(docs.RECORDING) | docs.errors(*SHARE_ERRORS, ErrorCode.RECORDING_EXPIRED),
)
async def get_shared_recording(
    share_id: str, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[RecordingOut]:
    _, meeting = await open_link(session, share_id)
    now = timer.utcnow()
    if recording_expired(meeting, now):
        raise ApiError(ErrorCode.RECORDING_EXPIRED)
    return ok(await asyncio.to_thread(recording_of, meeting, now))  # 서명은 이벤트 루프 밖에서(API-13 과 같음)
