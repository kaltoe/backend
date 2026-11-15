"""API-09 사담 경고 닫기. POST /api/v1/meetings/{meetingId}/offtopic-events/{eventId}/dismiss (참가자)

[관련 있는 얘기예요]. 처음 닫은 사람과 시각을 남기고 전원에게 offtopic.dismissed 를 보낸다.
이미 닫힌 경고면 다시 보내지 않고 같은 200(멱등). 닫는 조건을 UPDATE 하나에 넣어 여러 명이 동시에 눌러도 한 번만 닫힌다.
닫은 뒤 같은 안건에서는 OFFTOPIC_DISMISS_QUIET_SECONDS(기본 60초) 동안 사담 판정을 쉰다(app/ai/offtopic.py).
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.auth import CurrentCaller
from app.api.errors import ApiError, ErrorCode
from app.api.schemas import CamelModel, Envelope, KstDatetime, ok
from app.db.models import OfftopicEvent, Participant
from app.db.session import get_db
from app.meetings import timer
from app.realtime.events import DismissedBy, OfftopicDismissed
from app.realtime.manager import manager

router = APIRouter()


class Dismissed(CamelModel):
    event_id: int
    dismissed_at: KstDatetime


@router.post(
    "/meetings/{meeting_id}/offtopic-events/{event_id}/dismiss",
    summary="API-09 사담 경고 닫기",
    description=(
        "사담 팝업의 [관련 있는 얘기예요]. 참가자 토큰이 필요하고 body 는 없습니다. "
        "처음 닫은 사람과 시각을 남기고 전원에게 WS-01 `offtopic.dismissed`를 보내 팝업을 닫습니다. "
        "이미 닫힌 경고면 다시 보내지 않고 같은 200을 줍니다(여러 명이 동시에 눌러도 오류 없음). "
        "닫은 뒤 같은 안건에서는 60초(서버 설정 `OFFTOPIC_DISMISS_QUIET_SECONDS`) 동안 경고를 다시 내지 않습니다."
    ),
    responses=docs.success(docs.DISMISSED)
    | docs.errors(
        ErrorCode.VALIDATION_ERROR, ErrorCode.UNAUTHORIZED, ErrorCode.OFFTOPIC_EVENT_NOT_FOUND, field="eventId"
    ),
)
async def dismiss(
    meeting_id: str, event_id: int, caller: CurrentCaller, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[Dismissed]:
    now = timer.utcnow()
    first = await session.scalar(
        update(OfftopicEvent)
        .where(
            OfftopicEvent.id == event_id,
            OfftopicEvent.meeting_id == meeting_id,
            OfftopicEvent.dismissed_at.is_(None),
        )
        .values(dismissed_by=caller.participant_id, dismissed_at=now)
        .returning(OfftopicEvent.dismissed_at)
    )
    if first is not None:
        await session.commit()
        participant = await session.get(Participant, caller.participant_id)
        dismissed_by = DismissedBy(participant_id=caller.participant_id, name=participant.name if participant else "")
        await manager.broadcast(
            meeting_id, "offtopic.dismissed", OfftopicDismissed(event_id=event_id, dismissed_by=dismissed_by)
        )
        return ok(Dismissed(event_id=event_id, dismissed_at=first))

    # 이미 닫혔거나 이 회의의 경고가 아니다
    dismissed_at = await session.scalar(
        select(OfftopicEvent.dismissed_at).where(OfftopicEvent.id == event_id, OfftopicEvent.meeting_id == meeting_id)
    )
    if dismissed_at is None:
        raise ApiError(ErrorCode.OFFTOPIC_EVENT_NOT_FOUND)
    return ok(Dismissed(event_id=event_id, dismissed_at=dismissed_at))
