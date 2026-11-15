"""API-14 전체 기록 조회. GET /api/v1/meetings/{meetingId}/transcripts?source=AUTO (참가자)

결과 화면 [전체 기록 · 전체 보기]. 발화를 안건별로 묶어 녹음 위치(offset_ms) 순으로 준다.
- AUTO 면 CLOVA 후처리가 DONE 일 때 POSTPROCESS, 아니면 REALTIME
- speakerLabel 은 저장된 값 그대로다. 대면 POSTPROCESS "화자 N", 대면 REALTIME null, 비대면 참가자 이름
- 어느 안건 구간에도 들지 않은 CLOVA 발화(agenda_id NULL)는 바로 앞 발화의 안건에, 맨 앞이면 첫 안건에 넣는다
- 발화가 없는 안건은 빠진다
group_transcripts() 는 공유 전체 기록(API-17, #32)에서도 쓴다.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.auth import CurrentCaller
from app.api.errors import ApiError, ErrorCode
from app.api.schemas import CamelModel, Envelope, ok
from app.db.models import Agenda, Meeting, PostprocessStatus, Utterance, UtteranceSource
from app.db.session import get_db

router = APIRouter()


class TranscriptItem(CamelModel):
    utterance_id: int
    speaker_label: str | None
    text: str
    offset_ms: int


class TranscriptGroup(CamelModel):
    agenda_id: int
    order_index: int
    title: str
    items: list[TranscriptItem]


class TranscriptsOut(CamelModel):
    source: UtteranceSource
    groups: list[TranscriptGroup]


def pick_source(meeting: Meeting, requested: str) -> UtteranceSource:
    if requested != "AUTO":
        return UtteranceSource(requested)
    done = meeting.postprocess_status == PostprocessStatus.DONE
    return UtteranceSource.POSTPROCESS if done else UtteranceSource.REALTIME


async def group_transcripts(session: AsyncSession, meeting: Meeting, requested: str = "AUTO") -> TranscriptsOut:
    """API-14·17 공통 응답. 인덱스 (meeting_id, source, offset_ms) 를 탄다."""
    source = pick_source(meeting, requested)
    agendas = (
        await session.scalars(select(Agenda).where(Agenda.meeting_id == meeting.id).order_by(Agenda.order_index))
    ).all()
    utterances = await session.scalars(
        select(Utterance)
        .where(Utterance.meeting_id == meeting.id, Utterance.source == source.value)
        .order_by(Utterance.offset_ms, Utterance.id)
    )
    items: dict[int, list[TranscriptItem]] = {agenda.id: [] for agenda in agendas}
    last = agendas[0].id if agendas else None
    for u in utterances:
        if u.agenda_id in items:
            last = u.agenda_id
        if last is None:
            continue
        items[last].append(
            TranscriptItem(utterance_id=u.id, speaker_label=u.speaker_label, text=u.text, offset_ms=u.offset_ms)
        )
    groups = [
        TranscriptGroup(agenda_id=a.id, order_index=a.order_index, title=a.title, items=items[a.id])
        for a in agendas
        if items[a.id]
    ]
    return TranscriptsOut(source=source, groups=groups)


@router.get(
    "/meetings/{meeting_id}/transcripts",
    summary="API-14 전체 기록 조회",
    description=(
        "결과 화면 [전체 기록]. 안건별로 묶은 발화를 녹음 위치 순으로 줍니다. `source` 기본값 `AUTO`는 "
        "후처리가 끝났으면(`postprocessStatus` DONE) `POSTPROCESS`, 아니면 `REALTIME`입니다. "
        "문장을 누르면 API-13 녹음의 재생 위치를 `offsetMs / 1000`초로 옮깁니다."
    ),
    responses=docs.success(docs.TRANSCRIPTS)
    | docs.errors(ErrorCode.VALIDATION_ERROR, ErrorCode.UNAUTHORIZED, field="source"),
)
async def get_transcripts(
    meeting_id: str,
    caller: CurrentCaller,
    session: Annotated[AsyncSession, Depends(get_db)],
    source: Annotated[Literal["AUTO", "REALTIME", "POSTPROCESS"], Query()] = "AUTO",
) -> Envelope[TranscriptsOut]:
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None:  # 토큰은 이 회의 것이므로 없을 일이 거의 없다(API-04 와 같이 401)
        raise ApiError(ErrorCode.UNAUTHORIZED)
    return ok(await group_transcripts(session, meeting, source))
