"""ERD 명세서(노션 자료 창고 > ERD 명세서)의 테이블 표를 그대로 옮긴 모델.

- 시각은 모두 timestamptz(UTC 저장)
- *_ms 는 녹음 타임라인 위치(일시정지를 뺀 회의 경과 ms)
- 상태값은 varchar + CHECK 제약
- 진행 중 안건은 meetings 에 따로 두지 않고 agendas.status = IN_PROGRESS 인 행으로 찾는다
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, REAL
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MeetingMode(StrEnum):
    IN_PERSON = "IN_PERSON"
    REMOTE = "REMOTE"


class MeetingStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    ENDED = "ENDED"


class EndedBy(StrEnum):
    HOST = "HOST"
    AUTO = "AUTO"


class RecordingStatus(StrEnum):
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class PostprocessStatus(StrEnum):
    NONE = "NONE"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class SummaryStatus(StrEnum):
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class AgendaStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"


class Role(StrEnum):
    HOST = "HOST"
    MEMBER = "MEMBER"


class UtteranceSource(StrEnum):
    REALTIME = "REALTIME"
    POSTPROCESS = "POSTPROCESS"


def one_of(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{m.value}'" for m in enum)
    return f"{column} IN ({values})"


def tz() -> DateTime:
    return DateTime(timezone=True)


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (
        CheckConstraint(one_of("meeting_mode", MeetingMode), name="meeting_mode"),
        CheckConstraint(
            "target_total_minutes IS NULL OR (target_total_minutes BETWEEN 5 AND 180 AND target_total_minutes % 5 = 0)",
            name="target_total_minutes",
        ),
        CheckConstraint("planned_total_minutes BETWEEN 1 AND 180", name="planned_total_minutes"),
        CheckConstraint(one_of("status", MeetingStatus), name="status"),
        CheckConstraint("(status = 'PAUSED') = (paused_at IS NOT NULL)", name="paused_at"),
        CheckConstraint("paused_total_seconds >= 0", name="paused_total_seconds"),
        CheckConstraint("(status = 'ENDED') = (ended_at IS NOT NULL)", name="ended_at"),
        CheckConstraint(
            f"(status = 'ENDED') = (ended_by IS NOT NULL) AND (ended_by IS NULL OR {one_of('ended_by', EndedBy)})",
            name="ended_by",
        ),
        CheckConstraint("attendee_count IS NULL OR attendee_count BETWEEN 1 AND 20", name="attendee_count"),
        CheckConstraint(
            f"recording_status IS NULL OR {one_of('recording_status', RecordingStatus)}", name="recording_status"
        ),
        CheckConstraint(one_of("postprocess_status", PostprocessStatus), name="postprocess_status"),
        CheckConstraint(f"summary_status IS NULL OR {one_of('summary_status', SummaryStatus)}", name="summary_status"),
    )

    # 회의 코드. ABCDEFGHJKLMNPQRSTUVWXYZ23456789 중 6자
    id: Mapped[str] = mapped_column(String(6), primary_key=True)
    title: Mapped[str] = mapped_column(String(30))
    meeting_mode: Mapped[str] = mapped_column(String(12), server_default=MeetingMode.IN_PERSON.value)
    target_total_minutes: Mapped[int | None] = mapped_column(Integer)
    planned_total_minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), server_default=MeetingStatus.IN_PROGRESS.value)
    consent_agreed_at: Mapped[datetime] = mapped_column(tz())
    # 진행자 복구 키의 bcrypt 해시. 평문은 저장하지 않는다
    host_recovery_key_hash: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(tz())
    expires_at: Mapped[datetime] = mapped_column(tz())
    paused_at: Mapped[datetime | None] = mapped_column(tz())
    paused_total_seconds: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    ended_at: Mapped[datetime | None] = mapped_column(tz())
    ended_by: Mapped[str | None] = mapped_column(String(8))
    actual_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    attendee_count: Mapped[int | None] = mapped_column(Integer)
    recording_status: Mapped[str | None] = mapped_column(String(12))
    recording_m4a_s3_key: Mapped[str | None] = mapped_column(String(300))
    recording_duration_ms: Mapped[int | None] = mapped_column(Integer)
    postprocess_status: Mapped[str] = mapped_column(String(12), server_default=PostprocessStatus.NONE.value)
    summary_status: Mapped[str | None] = mapped_column(String(12))
    updated_at: Mapped[datetime] = mapped_column(tz(), server_default=func.now(), onupdate=func.now())


class Agenda(Base):
    __tablename__ = "agendas"
    __table_args__ = (
        UniqueConstraint("meeting_id", "order_index"),
        CheckConstraint("order_index >= 0", name="order_index"),
        CheckConstraint("allocated_minutes BETWEEN 1 AND 180", name="allocated_minutes"),
        CheckConstraint(one_of("status", AgendaStatus), name="status"),
        # 진행 중 안건은 회의마다 하나뿐이다(current_agenda 가 이 행을 찾는다)
        Index(
            "uq_agendas_meeting_id_in_progress",
            "meeting_id",
            unique=True,
            postgresql_where=text("status = 'IN_PROGRESS'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"))
    order_index: Mapped[int] = mapped_column(SmallInteger)
    title: Mapped[str] = mapped_column(String(20))
    keywords: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    allocated_minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), server_default=AgendaStatus.PENDING.value)
    recording_start_ms: Mapped[int | None] = mapped_column(Integer)
    recording_end_ms: Mapped[int | None] = mapped_column(Integer)
    summary_lines: Mapped[list[str] | None] = mapped_column(JSONB)


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (
        UniqueConstraint("meeting_id", "name"),
        CheckConstraint(one_of("role", Role), name="role"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"))
    name: Mapped[str] = mapped_column(String(10))
    # 숫자 4자리의 bcrypt 해시. NULL 이면 비밀번호 없이 들어온 사람(API-03 이어받기 규칙)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(tz(), server_default=func.now())


class Utterance(Base):
    __tablename__ = "utterances"
    __table_args__ = (
        CheckConstraint(one_of("source", UtteranceSource), name="source"),
        Index(None, "meeting_id", "source", "offset_ms"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"))
    agenda_id: Mapped[int | None] = mapped_column(ForeignKey("agendas.id"))
    # 말한 사람. 비대면 REALTIME 발화만 값이 있다
    participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id"))
    source: Mapped[str] = mapped_column(String(12))
    speaker_label: Mapped[str | None] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    offset_ms: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class OfftopicEvent(Base):
    __tablename__ = "offtopic_events"
    __table_args__ = (CheckConstraint("score BETWEEN 0 AND 1", name="score"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"))
    agenda_id: Mapped[int] = mapped_column(ForeignKey("agendas.id"))
    score: Mapped[float] = mapped_column(REAL)
    # 판정에 쓴 최근 30초 대화(오감지 분석용). 로그로 출력하지 않는다
    window_text: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(tz())
    dismissed_by: Mapped[int | None] = mapped_column(ForeignKey("participants.id"))
    dismissed_at: Mapped[datetime | None] = mapped_column(tz())


class ShareLink(Base):
    __tablename__ = "share_links"
    __table_args__ = (Index(None, "meeting_id", "expires_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"))
    share_id: Mapped[str] = mapped_column(String(22), unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("participants.id"))
    created_at: Mapped[datetime] = mapped_column(tz(), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(tz())
