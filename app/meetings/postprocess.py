"""회의 후 CLOVA 화자 분리(#26). 대면(IN_PERSON) 회의만 한다(비대면은 postprocess_status NONE).

요청: 합본(#25)이 만든 merged.flac 을 CLOVA 에 올린다. 화자 수는 셋업의 attendeeCount(없으면 자동).
userdata 에 meetingId 와 callbackToken = HMAC-SHA256(CLOVA_CALLBACK_SECRET, meetingId) 를 넣는다(저장하지 않음).
접수되면 merged.flac 을 지운다. 3번 실패하면 FAILED.

INT-01 콜백: 토큰을 다시 계산해 다르면 403. 후처리가 PROCESSING 일 때만 저장한다(중복 콜백은 두 번째부터 무시).
segments → utterances(POSTPROCESS). 합본 0ms = 회의 시작이라 start 를 그대로 offset_ms 로 쓰고,
어느 안건인지는 발화 시작이 안건 녹음 구간(recording_start_ms ≤ start < recording_end_ms)에 들어가는지로 정한다.
결과가 30분 넘게 오지 않으면 FAILED(정리 작업에서 본다).
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import summary
from app.config import get_settings
from app.db.models import (
    Agenda,
    Meeting,
    MeetingMode,
    PostprocessStatus,
    RecordingStatus,
    Utterance,
    UtteranceSource,
)
from app.db.session import SessionFactory, get_db
from app.stt import clova

logger = logging.getLogger(__name__)

router = APIRouter()

ATTEMPTS = 3
CALLBACK_TIMEOUT = timedelta(minutes=30)


def callback_token(meeting_id: str) -> str:
    secret = get_settings().clova_callback_secret
    return hmac.new(secret.encode(), meeting_id.encode(), hashlib.sha256).hexdigest()


def flac_of(meeting_id: str) -> Path:
    return Path(get_settings().recordings_dir) / meeting_id / "merged.flac"


async def set_status(sessions: SessionFactory, meeting_id: str, status: PostprocessStatus) -> None:
    async with sessions() as session:
        meeting = await session.get(Meeting, meeting_id)
        if meeting is not None:
            meeting.postprocess_status = status.value
            await session.commit()


async def request(sessions: SessionFactory, meeting_id: str) -> None:
    """합본이 끝난 뒤 부른다. merged.flac 을 CLOVA 에 올리고, 접수되면 지운다."""
    async with sessions() as session:
        meeting = await session.get(Meeting, meeting_id)
        if (
            meeting is None
            or meeting.meeting_mode == MeetingMode.REMOTE
            or meeting.postprocess_status != PostprocessStatus.PROCESSING
        ):
            return
        speakers = meeting.attendee_count
    flac = flac_of(meeting_id)
    if not flac.exists():  # 합본을 못 만들었다
        await set_status(sessions, meeting_id, PostprocessStatus.FAILED)
        return
    userdata = {"meetingId": meeting_id, "callbackToken": callback_token(meeting_id)}
    for attempt in range(1, ATTEMPTS + 1):
        try:
            await clova.request(flac, get_settings(), speakers, userdata)
        except Exception as exc:
            logger.warning("CLOVA 요청 실패(%d/%d): %s %s", attempt, ATTEMPTS, meeting_id, type(exc).__name__)
            continue
        flac.unlink(missing_ok=True)  # 다시 요청할 때는 S3 합본으로 새로 만든다
        return
    await set_status(sessions, meeting_id, PostprocessStatus.FAILED)


async def expire(sessions: SessionFactory, now: datetime) -> list[str]:
    """결과가 30분 넘게 오지 않은 회의를 FAILED 로 둔다. 합본이 아직 만드는 중이면 기다린다."""
    async with sessions() as session:
        stuck = list(
            await session.scalars(
                select(Meeting).where(
                    Meeting.postprocess_status == PostprocessStatus.PROCESSING.value,
                    Meeting.recording_status != RecordingStatus.PROCESSING.value,
                    Meeting.ended_at < now - CALLBACK_TIMEOUT,
                )
            )
        )
        expired = [m.id for m in stuck]  # 커밋하면 속성이 만료되므로 먼저 읽는다
        for meeting in stuck:
            meeting.postprocess_status = PostprocessStatus.FAILED.value
        await session.commit()
    return expired


def agenda_at(agendas: list[Agenda], start_ms: int) -> int | None:
    for agenda in agendas:
        if agenda.recording_start_ms is None or agenda.recording_end_ms is None:
            continue
        if agenda.recording_start_ms <= start_ms < agenda.recording_end_ms:
            return agenda.id
    return None


def read_userdata(body: dict[str, Any]) -> dict[str, Any]:
    userdata = body.get("userdata")
    if isinstance(userdata, str):  # 문자열로 돌려주는 경우
        try:
            userdata = json.loads(userdata)
        except ValueError:
            return {}
    return userdata if isinstance(userdata, dict) else {}


@router.post(
    "/internal/clova/callback",
    summary="INT-01 CLOVA 결과 콜백",
    description=(
        "CLOVA 비동기 인식이 끝나면 네이버가 부르는 내부 API입니다. 프론트는 부르지 않습니다. "
        '공통 응답 형식을 쓰지 않고 `{ "received": true }`로 답합니다. '
        "`userdata.callbackToken`이 서버 계산값과 다르면 403입니다. 후처리가 진행 중일 때만 저장하고, "
        "같은 콜백이 다시 오면 저장하지 않고 200입니다."
    ),
    responses={
        200: {"content": {"application/json": {"example": {"received": True}}}},
        403: {"description": "callbackToken 불일치", "content": {"application/json": {"example": {"received": False}}}},
    },
)
async def clova_callback(request: Request, session: Annotated[AsyncSession, Depends(get_db)]) -> JSONResponse:
    try:
        body: Any = await request.json()
    except ValueError:
        body = None
    userdata = read_userdata(body) if isinstance(body, dict) else {}
    meeting_id, token = userdata.get("meetingId"), userdata.get("callbackToken")
    if (
        not get_settings().clova_callback_secret
        or not isinstance(meeting_id, str)
        or not isinstance(token, str)
        or not hmac.compare_digest(token, callback_token(meeting_id))
    ):
        return JSONResponse({"received": False}, status_code=403)
    assert isinstance(body, dict)

    # 같은 콜백이 동시에 와도 하나만 저장하게 회의 행을 잠근다
    meeting = await session.scalar(select(Meeting).where(Meeting.id == meeting_id).with_for_update())
    if meeting is None or meeting.postprocess_status != PostprocessStatus.PROCESSING:
        return JSONResponse({"received": True})
    try:
        segments = clova.parse_response(body)
    except (clova.ClovaError, KeyError, TypeError):
        logger.warning("CLOVA 결과 실패: %s result=%s", meeting_id, body.get("result"))
        meeting.postprocess_status = PostprocessStatus.FAILED.value
        await session.commit()
        return JSONResponse({"received": True})

    agendas = list(await session.scalars(select(Agenda).where(Agenda.meeting_id == meeting_id)))
    for segment in segments:
        session.add(
            Utterance(
                meeting_id=meeting_id,
                agenda_id=agenda_at(agendas, segment.start_ms),
                source=UtteranceSource.POSTPROCESS.value,
                speaker_label=f"화자 {segment.speaker}" if segment.speaker else None,
                text=segment.text,
                offset_ms=segment.start_ms,
                duration_ms=segment.end_ms - segment.start_ms,
            )
        )
    meeting.postprocess_status = PostprocessStatus.DONE.value
    await session.commit()
    summary.refresh_later(meeting_id)  # 종료 직후 실시간 발화로 만든 요약을 화자 분리 결과로 다시 만든다(#27)
    return JSONResponse({"received": True})
