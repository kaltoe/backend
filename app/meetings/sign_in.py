"""API-03 참여 Sign In. POST /api/v1/meetings/{meetingId}/sign-in (토큰 불필요)

이름 규칙 (노션 API-03 비고)
1. 처음 보는 이름 → 새 MEMBER. 비밀번호가 있으면 해시 저장. isReturning false
2. 비밀번호가 있는 이름 → 맞으면 같은 참가자로 재입장, 틀리거나 빠지면 401 INVALID_PASSWORD
3. 비밀번호 없이 등록된 이름 → 그 참가자가 지금 접속 중(WS-01)이 아니면 이어받음, 접속 중이면 409 NAME_TAKEN.
   이때 요청의 비밀번호는 무시하고 저장하지 않는다
4. 진행자 이름은 이어받을 수 없어 409 NAME_TAKEN

진행자 복구: hostRecoveryKey 가 있으면 이름·비밀번호는 보지 않고 키로 기존 진행자에게 새 토큰을 준다.
"""

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import Field, ValidationInfo, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import create_access_token
from app.api.errors import VALIDATION_MESSAGE, ApiError, ErrorCode
from app.api.rate_limit import limit_entry_requests
from app.api.schemas import CamelModel, Envelope, ok
from app.db.models import MeetingStatus, Participant, Role
from app.db.session import get_db
from app.meetings.attempts import password_failures, recovery_key_failures
from app.meetings.codes import CODE_ALPHABET, hash_secret, verify_secret
from app.meetings.entry import ensure_joinable, find_meeting
from app.meetings.timer import utcnow
from app.realtime.manager import manager

router = APIRouter()

PASSWORD = re.compile(r"^\d{4}$")
RECOVERY_KEY = re.compile(f"^[{CODE_ALPHABET}]{{8}}$")


def _blank_to_none(value: str | None) -> str | None:
    # 입력칸을 비워 보낸 "" 는 생략한 것으로 본다
    return None if value is None or value.strip() == "" else value


class SignInRequest(CamelModel):
    # 복구 키가 있으면 이름·비밀번호는 검사하지 않으므로 먼저 둔다
    host_recovery_key: str | None = None
    # 생략해도 검증 함수를 돌려야 "이름 없음"이 400 이 된다
    name: str | None = Field(default=None, validate_default=True)
    password: str | None = None

    @field_validator("host_recovery_key")
    @classmethod
    def _key(cls, value: str | None) -> str | None:
        return _blank_to_none(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None, info: ValidationInfo) -> str | None:
        if info.data.get("host_recovery_key") is not None:
            return value
        value = (value or "").strip()
        if not 1 <= len(value) <= 10:
            raise ValueError(VALIDATION_MESSAGE)
        return value

    @field_validator("password")
    @classmethod
    def _password(cls, value: str | None, info: ValidationInfo) -> str | None:
        value = _blank_to_none(value)
        if info.data.get("host_recovery_key") is not None:
            return None
        if value is not None and not PASSWORD.match(value):
            raise ValueError(VALIDATION_MESSAGE)
        return value


class SignedIn(CamelModel):
    participant_id: int
    name: str
    role: Role
    is_returning: bool
    meeting_status: Literal["IN_PROGRESS", "PAUSED"]
    access_token: str


def normalize_recovery_key(raw: str) -> str | None:
    """대소문자와 하이픈(과 공백)은 무시한다. 형식이 아니면 None."""
    compact = re.sub(r"[\s-]", "", raw).upper()
    if not RECOVERY_KEY.match(compact):
        return None
    return f"{compact[:4]}-{compact[4:]}"


async def recover_host(session: AsyncSession, meeting_id: str, key_hash: str, raw_key: str) -> Participant:
    lock_key = (meeting_id,)
    if not recovery_key_failures.attempt(lock_key):
        raise ApiError(ErrorCode.TOO_MANY_ATTEMPTS)
    key = normalize_recovery_key(raw_key)
    if key is None or not await verify_secret(key, key_hash):
        raise ApiError(ErrorCode.INVALID_RECOVERY_KEY)
    recovery_key_failures.succeed(lock_key)
    host = await session.scalar(
        select(Participant).where(Participant.meeting_id == meeting_id, Participant.role == Role.HOST.value)
    )
    if host is None:  # 회의 생성 때 함께 만들므로 일어나지 않는다
        raise RuntimeError(f"진행자 참가자가 없습니다: {meeting_id}")
    return host


async def sign_in_by_name(
    session: AsyncSession, meeting_id: str, name: str, password: str | None
) -> tuple[Participant, bool]:
    existing = await session.scalar(
        select(Participant).where(Participant.meeting_id == meeting_id, Participant.name == name)
    )
    if existing is None:
        return await _new_member(session, meeting_id, name, password), False

    if existing.role == Role.HOST.value:  # 규칙 4
        raise ApiError(ErrorCode.NAME_TAKEN)

    if existing.password_hash is not None:  # 규칙 2
        lock_key = (meeting_id, name)
        if not password_failures.attempt(lock_key):
            raise ApiError(ErrorCode.TOO_MANY_ATTEMPTS)
        if password is None or not await verify_secret(password, existing.password_hash):
            raise ApiError(ErrorCode.INVALID_PASSWORD)
        password_failures.succeed(lock_key)
        return existing, True

    if manager.is_connected(meeting_id, existing.id):  # 규칙 3
        raise ApiError(ErrorCode.NAME_TAKEN)
    return existing, True


async def _new_member(session: AsyncSession, meeting_id: str, name: str, password: str | None) -> Participant:
    member = Participant(
        meeting_id=meeting_id,
        name=name,
        role=Role.MEMBER.value,
        password_hash=await hash_secret(password) if password is not None else None,
    )
    try:
        async with session.begin_nested():
            session.add(member)
    except IntegrityError as exc:
        # 같은 새 이름이 동시에 들어오면 먼저 들어온 사람만 만든다
        if "uq_participants_meeting_id_name" not in str(exc.orig):
            raise
        raise ApiError(ErrorCode.NAME_TAKEN) from exc
    return member


@router.post("/meetings/{meeting_id}/sign-in", dependencies=[Depends(limit_entry_requests)])
async def sign_in(
    meeting_id: str, body: SignInRequest, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[SignedIn]:
    meeting = ensure_joinable(await find_meeting(session, meeting_id), utcnow())

    if body.host_recovery_key is not None:
        participant = await recover_host(session, meeting.id, meeting.host_recovery_key_hash, body.host_recovery_key)
        returning = True
    else:
        assert body.name is not None  # 스키마 검증에서 보장
        participant, returning = await sign_in_by_name(session, meeting.id, body.name, body.password)

    result = ok(
        SignedIn(
            participant_id=participant.id,
            name=participant.name,
            role=Role(participant.role),
            is_returning=returning,
            meeting_status="PAUSED" if meeting.status == MeetingStatus.PAUSED else "IN_PROGRESS",
            access_token=create_access_token(participant.id, meeting.id, Role(participant.role).value),
        )
    )
    await session.commit()
    return result
