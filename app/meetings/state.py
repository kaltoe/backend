"""API-04 회의 현재 상태 조회: 새로고침·늦은 입장·WebSocket 재연결 때 라이브 화면 전체를 복원한다.

WebSocket 은 놓친 이벤트를 다시 보내지 않으므로 화면을 맞추는 기준은 이 응답 하나다.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.auth import CurrentCaller
from app.api.errors import ApiError, ErrorCode
from app.api.schemas import CamelModel, Envelope, KstDatetime, ok
from app.db.models import (
    Agenda,
    AgendaStatus,
    MeetingMode,
    MeetingStatus,
    Participant,
    Role,
    Utterance,
    UtteranceSource,
)
from app.db.session import get_db
from app.meetings.create import invite_url
from app.meetings.entry import find_meeting
from app.meetings.schemas import AgendaOut, InviteOut, ParticipantOut
from app.meetings.timer import TimerSnapshot, build_snapshot, utcnow
from app.realtime.events import MicState
from app.realtime.manager import manager
from app.stt import live

router = APIRouter()

TRANSCRIPT_LIMIT = 200

SttStatus = Literal["OK", "DELAYED", "NO_AUDIO"]
LiveRecordingStatus = Literal["RECORDING", "INTERRUPTED", "PAUSED"]
OfftopicStatus = Literal["OK", "ERROR"]


class StateAgenda(AgendaOut):
    actual_seconds: int | None  # 끝난 안건만. 진행·대기는 null


class Me(ParticipantOut):
    mic_state: MicState


class StateParticipant(ParticipantOut):
    is_connected: bool
    mic_state: MicState


class Transcript(CamelModel):
    utterance_id: int
    text: str
    offset_ms: int
    agenda_id: int  # 지금 안건으로 걸러 오므로 항상 값이 있다
    # 비대면이면 말한 참가자, 대면이면 null (WS-01 transcript.final 과 같은 규칙)
    participant_id: int | None
    speaker_name: str | None


class Recording(CamelModel):
    status: LiveRecordingStatus
    # 지금 기준 스트림 기기. INTERRUPTED·PAUSED 와 비대면(기준 기기 없음)은 null
    source_participant_id: int | None


class ActiveOfftopicEvent(CamelModel):
    event_id: int
    agenda_id: int
    agenda_title: str
    score: float
    detected_at: KstDatetime
    auto_close_seconds: int


class MeetingState(CamelModel):
    meeting_id: str
    title: str
    meeting_mode: MeetingMode
    status: MeetingStatus  # ENDED 여도 200. 프론트가 결과 화면으로 넘어간다
    connected_count: int
    invite: InviteOut
    timer: TimerSnapshot
    agendas: list[StateAgenda]
    me: Me
    participants: list[StateParticipant]
    transcripts: list[Transcript]  # 지금 안건의 확정 발화 중 최근 200개, 오래된 순
    stt_status: SttStatus
    recording: Recording
    offtopic_status: OfftopicStatus
    active_offtopic_event: ActiveOfftopicEvent | None


@router.get(
    "/meetings/{meeting_id}/state",
    summary="API-04 회의 현재 상태 조회",
    description=(
        "라이브 화면 마운트·새로고침·늦은 입장·WebSocket 재연결 때 화면 전체를 복원합니다. "
        "WebSocket은 놓친 이벤트를 다시 보내지 않으므로 이 응답이 기준입니다. "
        "`status`가 ENDED여도 200이고, 프론트가 결과 화면으로 보냅니다. "
        "`transcripts`는 지금 안건의 확정 발화 중 최근 200개(오래된 순)입니다. "
        "비대면 회의는 `participantId`·`speakerName`에 말한 사람이 들어갑니다."
    ),
    responses=docs.success(docs.STATE) | docs.errors(ErrorCode.UNAUTHORIZED),
)
async def get_state(
    meeting_id: str, caller: CurrentCaller, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[MeetingState]:
    meeting = await find_meeting(session, meeting_id)
    me = await session.get(Participant, caller.participant_id)
    # 명세 Status 는 200·401 뿐이다. 토큰은 그 회의에서만 나오므로
    # 회의나 참가자 행이 없으면 토큰이 맞지 않는 것으로 본다
    if meeting is None or me is None or me.meeting_id != meeting_id:
        raise ApiError(ErrorCode.UNAUTHORIZED)

    agendas = list(
        await session.scalars(select(Agenda).where(Agenda.meeting_id == meeting_id).order_by(Agenda.order_index))
    )
    current = next((a for a in agendas if a.status == AgendaStatus.IN_PROGRESS), None)
    participants = list(
        await session.scalars(select(Participant).where(Participant.meeting_id == meeting_id).order_by(Participant.id))
    )
    names = {p.id: p.name for p in participants}

    transcripts: list[Transcript] = []
    if current is not None:  # 끝난 회의는 진행 중 안건이 없어 빈 목록
        latest = await session.scalars(
            select(Utterance)
            .where(
                Utterance.meeting_id == meeting_id,
                Utterance.source == UtteranceSource.REALTIME.value,
                Utterance.agenda_id == current.id,
            )
            .order_by(Utterance.offset_ms.desc(), Utterance.id.desc())
            .limit(TRANSCRIPT_LIMIT)
        )
        transcripts = [
            Transcript(
                utterance_id=u.id,
                text=u.text,
                offset_ms=u.offset_ms,
                agenda_id=current.id,
                participant_id=u.participant_id,
                speaker_name=names.get(u.participant_id) if u.participant_id is not None else None,
            )
            for u in reversed(list(latest))
        ]

    def mic(participant_id: int) -> MicState:
        return manager.mic_state(meeting_id, participant_id)

    live_status = live.status(meeting_id)
    if live_status is None:  # 아직 소리가 오지 않았다(비대면은 #22)
        stt_status: SttStatus = "OK"
        recording = Recording(
            status="PAUSED" if meeting.status == MeetingStatus.PAUSED else "RECORDING", source_participant_id=None
        )
    else:
        stt_status, recording_status, source = live_status
        recording = Recording(status=recording_status, source_participant_id=source)

    return ok(
        MeetingState(
            meeting_id=meeting.id,
            title=meeting.title,
            meeting_mode=MeetingMode(meeting.meeting_mode),
            status=MeetingStatus(meeting.status),
            connected_count=manager.count(meeting_id),
            invite=InviteOut(code=meeting.id, url=invite_url(meeting.id), expires_at=meeting.expires_at),
            timer=build_snapshot(meeting, current, utcnow()),
            agendas=[
                StateAgenda(
                    agenda_id=a.id,
                    order_index=a.order_index,
                    title=a.title,
                    keywords=a.keywords,
                    allocated_seconds=a.allocated_minutes * 60,
                    status=AgendaStatus(a.status),
                    actual_seconds=(a.recording_end_ms - (a.recording_start_ms or 0)) // 1000
                    if a.status == AgendaStatus.DONE and a.recording_end_ms is not None
                    else None,
                )
                for a in agendas
            ],
            me=Me(participant_id=me.id, name=me.name, role=Role(me.role), mic_state=mic(me.id)),
            participants=[
                StateParticipant(
                    participant_id=p.id,
                    name=p.name,
                    role=Role(p.role),
                    is_connected=manager.is_connected(meeting_id, p.id),
                    mic_state=mic(p.id),
                )
                for p in participants
            ],
            transcripts=transcripts,
            stt_status=stt_status,
            recording=recording,
            # ponytail: 고정 기본값. #23(offtopic*)이 메모리 상태를 만들면 거기서 읽는다
            offtopic_status="OK",
            active_offtopic_event=None,
        )
    )
