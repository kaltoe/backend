"""API-11 요약 조회, API-12 요약 다시 생성. 요약은 app/ai/summary.py 가 만든다.

API-11 은 항상 200 이다. 결과 화면이 3초마다 부르므로 외부 AI 실패도 502 대신 200 + FAILED 로 알린다.
API-12 는 FAILED 일 때만 PROCESSING 으로 돌려 놓고 202. 실제 생성은 요약 루프가 몇 초 안에 집어 간다.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.auth import CurrentCaller
from app.api.errors import ApiError, ErrorCode
from app.api.schemas import CamelModel, Envelope, ok
from app.db.models import Agenda, AgendaStatus, Meeting, SummaryStatus
from app.db.session import get_db

router = APIRouter()

SummaryState = Literal["PROCESSING", "DONE", "FAILED"]


class AgendaSummary(CamelModel):
    agenda_id: int
    lines: list[str]


class MeetingSummary(CamelModel):
    status: SummaryState
    agendas: list[AgendaSummary]


class RetryAccepted(CamelModel):
    status: Literal["PROCESSING"]


@router.get(
    "/meetings/{meeting_id}/summary",
    summary="API-11 요약 조회",
    description=(
        "결과 화면 요약 카드. `PROCESSING`이면 3초마다 다시 부릅니다. 항상 200이고, 외부 AI 호출이 실패해도 "
        "502가 아니라 `status: FAILED`로 알립니다. `PROCESSING`·`FAILED`면 `agendas`는 빈 배열, "
        "`DONE`이면 완료된 안건이 순서대로 들어 있고 발언이 없던 안건은 `lines: []`입니다."
    ),
    responses=docs.success(docs.SUMMARY) | docs.errors(ErrorCode.UNAUTHORIZED),
)
async def get_summary(
    meeting_id: str, caller: CurrentCaller, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[MeetingSummary]:
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None:  # 토큰은 이 회의 것이므로 없을 일이 거의 없다(API-04 와 같이 401)
        raise ApiError(ErrorCode.UNAUTHORIZED)
    status: SummaryState = (
        "DONE"
        if meeting.summary_status == SummaryStatus.DONE
        else "FAILED"
        if meeting.summary_status == SummaryStatus.FAILED
        else "PROCESSING"  # 만드는 중이거나 아직 끝나지 않은 회의
    )
    agendas: list[AgendaSummary] = []
    if status == "DONE":
        done = await session.scalars(
            select(Agenda)
            .where(Agenda.meeting_id == meeting_id, Agenda.status == AgendaStatus.DONE.value)
            .order_by(Agenda.order_index)
        )
        agendas = [AgendaSummary(agenda_id=a.id, lines=a.summary_lines or []) for a in done]
    return ok(MeetingSummary(status=status, agendas=agendas))


@router.post(
    "/meetings/{meeting_id}/summary/retry",
    status_code=202,
    summary="API-12 요약 다시 생성",
    description=(
        "요약 실패 화면의 [다시 시도]. `FAILED`일 때만 받고 바로 202를 줍니다. 프론트는 API-11을 다시 확인합니다. "
        "이미 저장된 발화로만 다시 만들고 녹음을 다시 처리하지는 않습니다. "
        "`FAILED`가 아니거나 이미 만드는 중이면 409 `INVALID_STATE`입니다."
    ),
    responses=docs.success({"status": "PROCESSING"}, 202)
    | docs.errors(ErrorCode.UNAUTHORIZED, ErrorCode.INVALID_STATE),
)
async def retry_summary(
    meeting_id: str, caller: CurrentCaller, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[RetryAccepted]:
    # 동시에 눌러도 한 번만 받는다
    meeting = await session.scalar(select(Meeting).where(Meeting.id == meeting_id).with_for_update())
    if meeting is None:
        raise ApiError(ErrorCode.UNAUTHORIZED)
    if meeting.summary_status != SummaryStatus.FAILED:
        raise ApiError(ErrorCode.INVALID_STATE)
    meeting.summary_status = SummaryStatus.PROCESSING.value
    await session.commit()
    return ok(RetryAccepted(status="PROCESSING"))
