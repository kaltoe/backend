"""API-02 회의 생성 = 시작. POST /api/v1/meetings (토큰 불필요)

만드는 순간 회의와 타이머가 시작된다. 첫 안건만 IN_PROGRESS(recording_start_ms = 0), 나머지는 PENDING.
진행자 토큰과 진행자 복구 키는 이 응답에서만 나간다.
"""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.auth import create_access_token
from app.api.errors import ApiError, ErrorCode
from app.api.schemas import Envelope, ok
from app.config import get_settings
from app.db.models import Agenda, AgendaStatus, Meeting, MeetingStatus, Participant, Role
from app.db.session import get_db
from app.meetings import codes
from app.meetings.idempotency import create_meeting_cache
from app.meetings.schemas import AgendaOut, CreatedMeeting, CreateMeetingRequest, InviteOut, ParticipantOut
from app.meetings.timer import build_snapshot, utcnow

router = APIRouter()

MEETING_TTL = timedelta(days=14)
HOST_NAME = "진행자"
MAX_CODE_ATTEMPTS = 10


def invite_url(code: str) -> str:
    return f"{get_settings().frontend_base_url.rstrip('/')}/m/{code}"


async def insert_with_new_code(session: AsyncSession, meeting: Meeting) -> None:
    """회의 코드가 이미 있으면 새 코드로 다시 넣는다. 동시에 같은 코드가 나와도 PK 충돌로 잡힌다."""
    for _ in range(MAX_CODE_ATTEMPTS):
        meeting.id = codes.new_meeting_code()
        try:
            async with session.begin_nested():
                session.add(meeting)
        except IntegrityError as exc:
            if "pk_meetings" not in str(exc.orig):
                raise
            continue
        return
    raise RuntimeError("회의 코드를 만들지 못했습니다")


async def create(session: AsyncSession, body: CreateMeetingRequest, now: datetime) -> Envelope[CreatedMeeting]:
    recovery_key = codes.new_recovery_key()
    meeting = Meeting(
        title=body.title,
        meeting_mode=body.meeting_mode.value,
        target_total_minutes=body.target_total_minutes,
        planned_total_minutes=body.planned_total_minutes,
        status=MeetingStatus.IN_PROGRESS.value,
        consent_agreed_at=now,
        host_recovery_key_hash=await codes.hash_secret(recovery_key),
        started_at=now,
        expires_at=now + MEETING_TTL,
        paused_total_seconds=0,
        attendee_count=body.attendee_count,
    )
    await insert_with_new_code(session, meeting)

    agendas = [
        Agenda(
            meeting_id=meeting.id,
            order_index=i,
            title=a.title,
            keywords=a.keywords,
            allocated_minutes=a.allocated_minutes,
            status=(AgendaStatus.IN_PROGRESS if i == 0 else AgendaStatus.PENDING).value,
            recording_start_ms=0 if i == 0 else None,
        )
        for i, a in enumerate(body.agendas)
    ]
    host = Participant(meeting_id=meeting.id, name=HOST_NAME, role=Role.HOST.value)
    session.add_all([*agendas, host])
    await session.flush()

    # 커밋하면 객체가 만료될 수 있어 응답을 먼저 만든다
    result = ok(
        CreatedMeeting(
            meeting_id=meeting.id,
            title=meeting.title,
            meeting_mode=body.meeting_mode,
            status=MeetingStatus.IN_PROGRESS,
            invite=InviteOut(code=meeting.id, url=invite_url(meeting.id), expires_at=meeting.expires_at),
            timer=build_snapshot(meeting, agendas[0], now),
            agendas=[
                AgendaOut(
                    agenda_id=a.id,
                    order_index=a.order_index,
                    title=a.title,
                    keywords=a.keywords,
                    allocated_seconds=a.allocated_minutes * 60,
                    status=AgendaStatus(a.status),
                )
                for a in agendas
            ],
            host=ParticipantOut(participant_id=host.id, name=host.name, role=Role.HOST),
            access_token=create_access_token(host.id, meeting.id, "HOST", now),
            host_recovery_key=recovery_key,
        )
    )
    await session.commit()
    return result


@router.post(
    "/meetings",
    status_code=201,
    summary="API-02 회의 생성 = 시작",
    description=(
        "셋업의 [회의 시작하기]. 만드는 즉시 회의와 타이머가 시작되고(첫 안건만 진행 중), 진행자 토큰(`accessToken`)과 "
        '진행자 복구 키를 돌려줍니다. 진행자 이름은 "진행자"이고 초대 코드는 14일 뒤 만료됩니다.\n\n'
        "`Idempotency-Key`(선택): 셋업에 들어올 때 만든 UUID를 보내면, 10분 안에 같은 키가 다시 와도 "
        "회의를 새로 만들지 않고 처음 응답을 그대로 돌려줍니다."
    ),
    responses=docs.success(docs.CREATED, 201)
    | docs.errors(
        ErrorCode.VALIDATION_ERROR, ErrorCode.AGENDA_TIME_MISMATCH, ErrorCode.CONSENT_REQUIRED, field="title"
    ),
)
async def create_meeting(
    body: Annotated[CreateMeetingRequest, Body(openapi_examples=docs.CREATE_REQUEST)],
    session: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(max_length=128, description="중복 생성 방지용 UUID(선택)")] = None,
) -> Envelope[CreatedMeeting]:
    if body.consent_agreed is not True:
        raise ApiError(ErrorCode.CONSENT_REQUIRED)
    if body.target_total_minutes is not None and body.target_total_minutes != body.planned_total_minutes:
        raise ApiError(ErrorCode.AGENDA_TIME_MISMATCH)

    if not idempotency_key:
        return await create(session, body, utcnow())
    # 같은 키는 처음 응답을 그대로 돌려준다(그 사이 요청 내용이 달라도)
    async with create_meeting_cache.hold(idempotency_key):
        cached: Envelope[CreatedMeeting] | None = create_meeting_cache.get(idempotency_key)
        if cached is not None:
            return cached
        result = await create(session, body, utcnow())
        create_meeting_cache.put(idempotency_key, result)
        return result
