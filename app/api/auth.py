"""참가자 토큰(JWT)과 인증 의존성 (API 명세서 > 공통 규약).

- payload { participantId, meetingId, role }, 만료 14일
- REST: Authorization: Bearer {accessToken}. WebSocket: ?token={accessToken}
- 토큰 없음·만료·다른 회의 토큰은 401 UNAUTHORIZED, 진행자 전용 API를 MEMBER 가 부르면 403 HOST_ONLY
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Literal

import jwt
from fastapi import Depends, Header

from app.api.errors import ApiError, ErrorCode
from app.config import get_settings

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
TOKEN_TTL = timedelta(days=14)

RoleName = Literal["HOST", "MEMBER"]


@dataclass(frozen=True)
class Caller:
    participant_id: int
    meeting_id: str
    role: RoleName

    @property
    def is_host(self) -> bool:
        return self.role == "HOST"


@lru_cache
def _secret() -> str:
    secret = get_settings().jwt_secret
    if secret:
        return secret
    # 공개 저장소에 적힌 키를 쓰면 누구나 진행자 토큰을 만들 수 있어, 없으면 프로세스마다 무작위로 만든다.
    # 이 경우 서버가 다시 뜨면 기존 토큰은 모두 401 이 된다
    logger.warning("JWT_SECRET 이 비어 있어 임시 키를 씁니다. 서버를 다시 시작하면 기존 토큰이 무효가 됩니다.")
    return secrets.token_urlsafe(48)


def create_access_token(participant_id: int, meeting_id: str, role: RoleName, now: datetime | None = None) -> str:
    issued = now or datetime.now(UTC)
    payload = {
        "participantId": participant_id,
        "meetingId": meeting_id,
        "role": role,
        "iat": issued,
        "exp": issued + TOKEN_TTL,
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def read_token(token: str, meeting_id: str) -> Caller | None:
    """서명·만료·회의를 확인한다. 하나라도 틀리면 None."""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM], options={"require": ["exp"]})
    except jwt.PyJWTError:
        return None
    if payload.get("meetingId") != meeting_id or payload.get("role") not in ("HOST", "MEMBER"):
        return None
    participant_id = payload.get("participantId")
    if not isinstance(participant_id, int):
        return None
    return Caller(participant_id=participant_id, meeting_id=meeting_id, role=payload["role"])


def get_caller(meeting_id: str, authorization: Annotated[str | None, Header()] = None) -> Caller:
    """경로의 {meetingId} 회의 참가자만 통과시킨다."""
    scheme, _, token = (authorization or "").partition(" ")
    caller = read_token(token, meeting_id) if scheme.lower() == "bearer" and token else None
    if caller is None:
        raise ApiError(ErrorCode.UNAUTHORIZED)
    return caller


def get_host(caller: Annotated[Caller, Depends(get_caller)]) -> Caller:
    if not caller.is_host:
        raise ApiError(ErrorCode.HOST_ONLY)
    return caller


CurrentCaller = Annotated[Caller, Depends(get_caller)]
CurrentHost = Annotated[Caller, Depends(get_host)]
