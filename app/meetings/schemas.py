"""회의 API 요청·응답 모델 (API-02 등). 실패 문구는 노션 각 API의 Request 표 그대로다."""

from typing import Annotated, Any

from pydantic import Strict, field_validator

from app.api.errors import VALIDATION_MESSAGE
from app.api.schemas import CamelModel, KstDatetime
from app.db.models import AgendaStatus, MeetingMode, MeetingStatus, Role
from app.meetings.timer import TimerSnapshot

TITLE_MESSAGE = "회의 제목을 2자 이상 입력해주세요."
AGENDA_LIMIT_MESSAGE = "안건은 최대 5개까지 추가할 수 있어요."
AGENDA_TITLE_MESSAGE = "안건 내용을 입력해주세요."
KEYWORD_LIMIT_MESSAGE = "키워드는 안건마다 5개까지 추가할 수 있어요."
ALLOCATED_MESSAGE = "안건 시간은 1분 이상이에요."

MAX_AGENDAS = 5
MAX_KEYWORDS = 5
MAX_TOTAL_MINUTES = 180

# "10"·10.5·true 를 숫자로 바꿔 받지 않는다
StrictInt = Annotated[int, Strict()]


class AgendaIn(CamelModel):
    title: str
    keywords: list[str] = []
    allocated_minutes: StrictInt

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 20:
            raise ValueError(AGENDA_TITLE_MESSAGE)
        return value

    @field_validator("keywords")
    @classmethod
    def _keywords(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for keyword in (k.strip() for k in value):
            if not 1 <= len(keyword) <= 6:
                raise ValueError(VALIDATION_MESSAGE)
            if keyword not in cleaned:
                cleaned.append(keyword)
        if len(cleaned) > MAX_KEYWORDS:
            raise ValueError(KEYWORD_LIMIT_MESSAGE)
        return cleaned

    @field_validator("allocated_minutes")
    @classmethod
    def _allocated(cls, value: int) -> int:
        if value < 1:
            raise ValueError(ALLOCATED_MESSAGE)
        return value


class CreateMeetingRequest(CamelModel):
    title: str
    target_total_minutes: StrictInt | None = None
    meeting_mode: MeetingMode = MeetingMode.IN_PERSON
    attendee_count: StrictInt | None = None
    # true 가 아니면 400 CONSENT_REQUIRED 라서 형식 검사 대신 API 에서 확인한다
    consent_agreed: Any = None
    agendas: list[AgendaIn]

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not 2 <= len(value) <= 30:
            raise ValueError(TITLE_MESSAGE)
        return value

    @field_validator("target_total_minutes")
    @classmethod
    def _target(cls, value: int | None) -> int | None:
        if value is not None and not (5 <= value <= MAX_TOTAL_MINUTES and value % 5 == 0):
            raise ValueError(VALIDATION_MESSAGE)
        return value

    @field_validator("attendee_count")
    @classmethod
    def _attendees(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 20:
            raise ValueError(VALIDATION_MESSAGE)
        return value

    @field_validator("agendas", mode="before")
    @classmethod
    def _agenda_count(cls, value: Any) -> Any:
        # 개수 초과는 안건 하나하나를 검사하기 전에 먼저 알린다
        if isinstance(value, list) and len(value) > MAX_AGENDAS:
            raise ValueError(AGENDA_LIMIT_MESSAGE)
        return value

    @field_validator("agendas")
    @classmethod
    def _agenda_total(cls, value: list[AgendaIn]) -> list[AgendaIn]:
        if not value or sum(a.allocated_minutes for a in value) > MAX_TOTAL_MINUTES:
            raise ValueError(VALIDATION_MESSAGE)
        return value

    @property
    def planned_total_minutes(self) -> int:
        return sum(a.allocated_minutes for a in self.agendas)


class InviteOut(CamelModel):
    code: str
    url: str
    expires_at: KstDatetime


class AgendaOut(CamelModel):
    agenda_id: int
    order_index: int
    title: str
    keywords: list[str]
    allocated_seconds: int
    status: AgendaStatus


class ParticipantOut(CamelModel):
    participant_id: int
    name: str
    role: Role


class CreatedMeeting(CamelModel):
    meeting_id: str
    title: str
    meeting_mode: MeetingMode
    status: MeetingStatus
    invite: InviteOut
    timer: TimerSnapshot
    agendas: list[AgendaOut]
    host: ParticipantOut
    access_token: str
    host_recovery_key: str
