"""WS-01 이벤트 형식: 서버 → 클라이언트 메시지는 모두 { type, data, sentAt }.

다른 기능(일시 정지, 안건 전환, 자막, 사담 …)은 manager.broadcast(meetingId, type, data)로 보낸다.
data 는 dict 나 CamelModel. 시각은 한국 시간(+09:00) ISO 8601.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic_core import to_jsonable_python

from app.api.errors import GENERIC_MESSAGE
from app.api.schemas import CamelModel, KstDatetime, to_kst_iso
from app.db.models import EndedBy, Role
from app.meetings.timer import TimerSnapshot, utcnow


def event(type_: str, data: Any = None, now: datetime | None = None) -> dict[str, Any]:
    return {
        "type": type_,
        "data": to_jsonable_python(data, by_alias=True),
        "sentAt": to_kst_iso(now or utcnow()),
    }


class MicState(StrEnum):
    ON = "ON"
    OFF = "OFF"
    DENIED = "DENIED"


class ParticipantJoined(CamelModel):
    participant_id: int
    name: str
    role: Role
    connected_count: int


class ParticipantLeft(CamelModel):
    participant_id: int
    connected_count: int


class ParticipantUpdated(CamelModel):
    participant_id: int
    mic_state: MicState


class TimerSync(CamelModel):
    timer: TimerSnapshot


class MeetingPaused(CamelModel):
    paused_at: KstDatetime
    timer: TimerSnapshot


class MeetingResumed(CamelModel):
    resumed_at: KstDatetime
    timer: TimerSnapshot


class MeetingEnded(CamelModel):
    ended_at: KstDatetime
    ended_by: EndedBy
    actual_duration_seconds: int | None


class ErrorCode(StrEnum):
    INVALID_MESSAGE = "INVALID_MESSAGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


ERROR_MESSAGES = {
    ErrorCode.INVALID_MESSAGE: "처리할 수 없는 메시지예요.",
    ErrorCode.INTERNAL_ERROR: GENERIC_MESSAGE,
}


def error_event(code: ErrorCode) -> dict[str, Any]:
    return event("error", {"code": code.value, "message": ERROR_MESSAGES[code]})


# 연결 오류는 error 이벤트가 아니라 close code 로 알린다 (WS-01 Status)
CLOSE_UNAUTHORIZED = 4401  # 토큰 없음·만료. 재연결 안 함
CLOSE_NOT_FOUND = 4404  # 회의 없음
CLOSE_REPLACED = 4409  # WS-02 전용. 같은 참가자가 다른 기기에서 음성 연결해 이전 연결을 닫음
CLOSE_ENDED = 4410  # 회의 종료. 재연결 안 함
CLOSE_BAD_AUDIO_START = 1008  # WS-02 audio.start 형식이 틀렸거나 startOffsetMs 가 버퍼 범위(지금 ~ 10분 전) 밖
CLOSE_HEARTBEAT_TIMEOUT = 1001  # 60초 동안 메시지가 없음. 클라이언트는 다시 연결해도 된다
