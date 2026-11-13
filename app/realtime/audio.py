"""WS-02 음성 송출. wss://…/ws/meetings/{meetingId}/audio?token={accessToken}

연결 직후 텍스트 프레임 audio.start 한 번, 그 뒤로 바이너리 PCM(16kHz·LINEAR16·mono) 청크가 온다.
서버는 이 채널로 아무것도 보내지 않는다(이벤트는 WS-01).
음성 연결은 참가자당 하나라서 새 연결이 오면 이전 것을 4409 로 닫는다.

녹음: 모든 기기 소리를 RECORDINGS_DIR/{meetingId}/{participantId}.wav 의 녹음 위치(회의 타임라인 ms)에 쓴다.
비어 있는 구간은 0(무음)으로 채워지므로 WAV 의 0ms 가 회의 시작이다. 소리를 받은 구간은 같은 폴더 {participantId}.json.
- 일시 정지 중에 온 소리는 버린다
- 재연결이면 startOffsetMs 부터 이어 쓴다. 지금 위치보다 10분 넘게 앞선 값은 거부한다(1008)
- 지금 위치보다 1초 넘게 늦은 소리(재연결 버퍼)는 WAV 에만 쓰고 실시간 자막으로 넘기지 않는다
"""

import asyncio
import contextlib
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, WebSocket
from pydantic import StrictInt, ValidationError

from app.api.schemas import CamelModel
from app.config import get_settings
from app.db.models import Meeting, MeetingMode, MeetingStatus, Role
from app.db.session import SessionFactory, get_session_factory
from app.meetings import timer
from app.meetings.entry import find_meeting
from app.realtime.events import CLOSE_BAD_AUDIO_START, CLOSE_ENDED, CLOSE_NOT_FOUND, CLOSE_REPLACED
from app.realtime.ws import authenticate, receive_text
from app.stt import live as captions

router = APIRouter(tags=["realtime"])

BYTES_PER_MS = 32  # 16000Hz × 2바이트 × 1채널
WAV_HEADER = 44
BUFFER_LIMIT_MS = 10 * 60 * 1000  # 클라이언트 재연결 버퍼 최대 10분
LIVE_SLACK_MS = 1000  # 지금 위치와 이만큼 어긋나도 이어 쓴다(네트워크 지연)

# 회의 코드 → 참가자 → 지금 음성 연결
audio_sockets: dict[str, dict[int, WebSocket]] = {}


class AudioStart(CamelModel):
    type: Literal["audio.start"]
    sample_rate: Literal[16000]
    encoding: Literal["LINEAR16"]
    channels: Literal[1]
    start_offset_ms: StrictInt | None = None


@dataclass
class Track:
    """연결 하나의 쓰기 위치. 위치는 WAV 데이터 기준 바이트."""

    cursor: int | None = None
    caught_up: bool = False  # 재연결 버퍼를 다 따라잡았는지
    spans: list[list[int]] = field(default_factory=list)  # 소리를 받은 구간 [startMs, endMs]

    def place(self, size: int, live_ms: int) -> tuple[int, bool]:
        """청크를 쓸 위치와 늦은 소리인지를 정하고 위치를 앞으로 옮긴다."""
        live, slack = live_ms * BYTES_PER_MS, LIVE_SLACK_MS * BYTES_PER_MS
        # 실시간으로 받다가 마이크를 끄거나 네트워크가 멈춰 지금 위치와 벌어지면 지금 위치로 맞춘다
        if self.cursor is None or (self.caught_up and abs(self.cursor + size - live) > slack):
            self.cursor = max(live - size, 0)
        at = self.cursor
        self.cursor += size
        late = self.cursor < live - slack
        self.caught_up = self.caught_up or not late

        start, end = at // BYTES_PER_MS, self.cursor // BYTES_PER_MS
        if self.spans and self.spans[-1][0] <= start <= self.spans[-1][1]:
            self.spans[-1][1] = max(self.spans[-1][1], end)
        else:
            self.spans.append([start, end])
        return at, late


def wav_header(data_size: int) -> bytes:
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        *(b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16, 1, 1, 16000, 16000 * 2, 2, 16, b"data", data_size),
    )


def write_pcm(path: Path, offset: int, pcm: bytes) -> None:
    """offset 위치에 쓴다. 파일 끝보다 뒤면 그 사이는 0(무음)으로 채워진다. 헤더 크기는 매번 고친다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("r+b" if path.exists() else "w+b") as f:
        f.seek(WAV_HEADER + offset)
        f.write(pcm)
        size = f.seek(0, 2) - WAV_HEADER
        f.seek(0)
        f.write(wav_header(size))


def save_spans(path: Path, spans: list[list[int]]) -> None:
    """이전 연결들이 남긴 구간과 합쳐 저장한다."""
    old = json.loads(path.read_text()) if path.exists() else []
    merged: list[list[int]] = []
    for start, end in sorted([[s["startMs"], s["endMs"]] for s in old] + spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    path.write_text(json.dumps([{"startMs": s, "endMs": e} for s, e in merged]))


async def load(sessions: SessionFactory, meeting_id: str) -> Meeting | None:
    async with sessions() as session:
        return await find_meeting(session, meeting_id)


async def close_all(meeting_id: str) -> None:
    """회의가 끝나면 부른다. 음성 연결을 모두 4410 으로 닫는다."""
    for websocket in list(audio_sockets.pop(meeting_id, {}).values()):
        with contextlib.suppress(Exception):
            await websocket.close(code=CLOSE_ENDED)


@router.websocket("/ws/meetings/{meeting_id}/audio")
async def audio_channel(
    websocket: WebSocket,
    meeting_id: str,
    sessions: Annotated[SessionFactory, Depends(get_session_factory)],
    token: str | None = None,
) -> None:
    await websocket.accept()
    member = await authenticate(sessions, meeting_id, token)
    if isinstance(member, int):
        await websocket.close(code=member)
        return
    try:
        start = AudioStart.model_validate_json(await receive_text(websocket) or "")
    except ValidationError:
        await websocket.close(code=CLOSE_BAD_AUDIO_START)
        return
    meeting = await load(sessions, meeting_id)
    if meeting is None:
        await websocket.close(code=CLOSE_NOT_FOUND)
        return
    offset, live = start.start_offset_ms, timer.timeline_ms(meeting, timer.utcnow())
    if offset is not None and not live - BUFFER_LIMIT_MS <= offset <= live:
        await websocket.close(code=CLOSE_BAD_AUDIO_START)
        return

    pid = member.participant_id
    sockets = audio_sockets.setdefault(meeting_id, {})
    previous, sockets[pid] = sockets.get(pid), websocket
    if previous is not None:
        with contextlib.suppress(Exception):
            await previous.close(code=CLOSE_REPLACED)

    folder = Path(get_settings().recordings_dir) / meeting_id
    track = Track(cursor=None if offset is None else offset * BYTES_PER_MS)
    try:
        while (message := await websocket.receive())["type"] != "websocket.disconnect":
            pcm = message.get("bytes")
            if not pcm:
                continue
            # ponytail: 청크마다 회의 행을 읽는다. 기기가 많아 DB 가 버거우면 pause·resume 에서 메모리 시계를 갱신한다
            meeting = await load(sessions, meeting_id)
            if meeting is None or meeting.status == MeetingStatus.ENDED:
                await websocket.close(code=CLOSE_ENDED)
                break
            if meeting.status == MeetingStatus.PAUSED:
                continue
            pcm = pcm[: len(pcm) // 2 * 2]  # 샘플(2바이트) 단위로 맞춘다
            at, late = track.place(len(pcm), timer.timeline_ms(meeting, timer.utcnow()))
            await asyncio.to_thread(write_pcm, folder / f"{pid}.wav", at, pcm)
            if not late:  # 늦은 소리는 자막에 넣지 않는다
                remote = member.name if meeting.meeting_mode == MeetingMode.REMOTE else None
                await captions.feed(sessions, meeting_id, pid, member.role == Role.HOST, pcm, at, remote)
    finally:
        current = audio_sockets.get(meeting_id, {})
        if current.get(pid) is websocket:
            del current[pid]
            if not current:
                audio_sockets.pop(meeting_id, None)
            await captions.leave(meeting_id, pid)
        if track.spans:
            await asyncio.to_thread(save_spans, folder / f"{pid}.json", track.spans)
