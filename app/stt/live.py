"""대면(IN_PERSON) 회의 실시간 자막(#21). 한 방의 기기들이 같은 소리를 받으므로
기준 기기 하나의 소리만 리턴제로에 넣는다.

기준 / 대체 스트림 (WS-02 표)
- 후보: 마이크 ON + 음성 소켓 연결 + 최근 3초 안 프레임
- 우선순위: 진행자, 아니면 최근 10초 평균 음량이 가장 큰 기기
- 기준이 후보에서 빠지면 진행 중 문장을 Finalize 하고 다음 후보를 같은 세션에 이어 넣는다
- 대체 기기를 쓰는 중 진행자 프레임이 3초 이어지면 진행자로 돌아간다. 대체끼리는 지금 기기가 빠질 때만 바꾼다
- 후보가 없으면 INTERRUPTED. 50초 넘으면 세션을 닫고(EOS) 후보가 생기면 새 세션을 연다
- 기준이 바뀔 때마다 WS-01 recording.status

리턴제로 시각은 그 세션에 넣은 소리 기준이다. 넣는 소리가 녹음 위치에서 이어지지 않을 때마다 기준점
(세션 위치, 녹음 위치)을 적어 두고, offsetMs = 기준점 녹음 위치 + (엔진 시작 시각 − 기준점 세션 위치) 로 바꾼다.

서버 한 대 기준의 메모리 상태다(WS-01 manager 와 같음).
"""

import asyncio
import contextlib
import logging
import math
import time
from array import array
from bisect import bisect_right
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import get_settings
from app.db.models import Utterance, UtteranceSource
from app.db.session import SessionFactory
from app.meetings import timer
from app.realtime.events import (
    MicState,
    RecordingState,
    RecordingStatusChanged,
    SttState,
    SttStatusChanged,
    TranscriptFinal,
    TranscriptPartial,
)
from app.realtime.manager import manager
from app.stt import rtzr

logger = logging.getLogger(__name__)

BYTES_PER_MS = 32  # 16kHz × 2바이트 × 1채널
CANDIDATE_SECONDS = 3.0
HOST_RETURN_SECONDS = 3.0
RUN_BREAK_SECONDS = 1.0  # 프레임이 이만큼 끊기면 '이어진' 것이 아니다
VOLUME_SECONDS = 10.0
NO_SOURCE_CLOSE_SECONDS = 50.0
NO_AUDIO_SECONDS = 30.0
DELAYED_SECONDS = 5.0
RETRY_SECONDS = 5.0  # 세션을 열지 못하면 이만큼 뒤에 다시 연다
TICK_SECONDS = 1.0
# ponytail: '낮은 음량' 기준(16bit RMS). 실제 회의실 기기로 듣고 조정한다
QUIET_RMS = 300.0

clock = time.monotonic


class SttSession(Protocol):
    async def send(self, pcm: bytes) -> None: ...
    async def finalize(self) -> None: ...
    async def close(self) -> None: ...
    def results(self) -> AsyncIterator[dict[str, Any]]: ...


async def open_rtzr() -> SttSession:
    return await rtzr.open_session(get_settings())


open_session: Callable[[], Awaitable[SttSession]] = open_rtzr

# 끝까지 돌아야 하는 작업(결과 읽기)의 참조. 참조가 없으면 asyncio 가 지울 수 있다
background: set[asyncio.Task[None]] = set()


def spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    task = asyncio.create_task(coro)
    background.add(task)
    task.add_done_callback(background.discard)
    return task


def rms(pcm: bytes) -> float:
    samples = array("h", pcm)  # LINEAR16 little-endian. 서버(x86·ARM)도 little-endian
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


@dataclass
class Device:
    is_host: bool
    last_frame: float = -math.inf
    run_start: float = 0.0  # 프레임이 끊기지 않고 이어지기 시작한 때
    volumes: deque[tuple[float, float]] = field(default_factory=deque)

    def hear(self, now: float, volume: float) -> None:
        if now - self.last_frame > RUN_BREAK_SECONDS:
            self.run_start = now
        self.last_frame = now
        self.volumes.append((now, volume))
        while now - self.volumes[0][0] > VOLUME_SECONDS:
            self.volumes.popleft()

    def volume(self, now: float, seconds: float = VOLUME_SECONDS) -> float:
        recent = [v for t, v in self.volumes if now - t <= seconds]
        return sum(recent) / len(recent) if recent else 0.0


@dataclass
class Run:
    """리턴제로 세션 하나."""

    number: int
    session: SttSession
    sent: int = 0  # 넣은 소리(바이트)
    anchors: list[tuple[int, int]] = field(default_factory=list)  # (세션 위치 ms, 녹음 위치 ms)
    next_at: int | None = None  # 이어지는 다음 청크의 녹음 위치(바이트)

    def offset_ms(self, start_at: int) -> int:
        i = max(bisect_right(self.anchors, (start_at, math.inf)) - 1, 0)
        session_ms, timeline_ms = self.anchors[i]
        return timeline_ms + start_at - session_ms


class Live:
    def __init__(
        self,
        meeting_id: str,
        sessions: SessionFactory,
        speaker: tuple[int, str] | None = None,
        notify: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """speaker 가 있으면 비대면 기기 하나의 세션이다. 자막에 그 참가자를 붙이고,
        상태가 바뀌면 직접 보내지 않고 notify 로 회의 전체 상태(Remote)를 다시 계산하게 한다."""
        self.meeting_id = meeting_id
        self.sessions = sessions
        self.speaker = speaker
        self.notify = notify
        self.devices: dict[int, Device] = {}
        self.source: int | None = None
        self.recording: RecordingState = "INTERRUPTED"
        self.stt: SttState = "OK"
        self.run: Run | None = None
        self.runs = 0
        self.failed = False  # 세션을 열지 못했거나 엔진이 먼저 끊었다(다시 붙는 중)
        self.retry_at = 0.0
        self.paused = False
        self.agenda_id: int | None = None
        now = clock()
        self.no_source_since: float | None = now
        self.last_result = now  # 엔진 메시지를 마지막으로 받은 때
        self.last_final = now
        self.lock = asyncio.Lock()
        self.ticker = asyncio.create_task(self.tick_loop())

    # 바깥에서 부르는 것. 모두 lock 안에서 돈다

    async def feed(self, participant_id: int, is_host: bool, pcm: bytes, at: int) -> None:
        """실시간으로 온 청크. at 은 녹음 위치(바이트)."""
        async with self.lock:
            now = clock()
            if self.paused:  # 재개 후 첫 소리
                self.paused = False
                self.no_source_since = self.last_result = self.last_final = now
            device = self.devices.setdefault(participant_id, Device(is_host))
            device.hear(now, rms(pcm))
            await self.choose(now)
            if participant_id == self.source:
                await self.send(pcm, at, now)

    async def leave(self, participant_id: int) -> None:
        async with self.lock:
            self.devices.pop(participant_id, None)
            await self.choose(clock())

    async def pause(self) -> None:
        async with self.lock:
            self.paused = True
            await self.stop_run()
            self.source = None
            await self.set_recording("PAUSED")

    async def end(self) -> None:
        async with self.lock:
            self.ticker.cancel()
            await self.stop_run()

    async def tick(self) -> None:
        async with self.lock:
            if self.paused:
                return
            now = clock()
            await self.choose(now)
            idle = self.no_source_since is not None and now - self.no_source_since > NO_SOURCE_CLOSE_SECONDS
            if self.source is None and idle:
                await self.stop_run()
            await self.update_stt(now)

    # 안에서만 쓰는 것

    async def tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await self.tick()
            except Exception:
                logger.exception("실시간 자막 검사 실패: %s", self.meeting_id)

    def candidate(self, participant_id: int, now: float) -> bool:
        device = self.devices.get(participant_id)
        return (
            device is not None
            and now - device.last_frame <= CANDIDATE_SECONDS
            and manager.mic_state(self.meeting_id, participant_id) == MicState.ON
        )

    async def choose(self, now: float) -> None:
        if self.paused:
            return
        host = next((p for p, d in self.devices.items() if d.is_host), None)
        current = self.source
        if current is not None and self.candidate(current, now):
            if current == host or host is None or not self.candidate(host, now):
                return
            hosting = self.devices[host]
            if now - hosting.last_frame > RUN_BREAK_SECONDS or now - hosting.run_start < HOST_RETURN_SECONDS:
                return
            new: int | None = host  # 진행자 프레임이 3초 이어졌다
        else:
            candidates = [p for p in self.devices if self.candidate(p, now)]
            new = (
                host if host in candidates else max(candidates, key=lambda p: self.devices[p].volume(now), default=None)
            )
        if new != current:
            await self.switch(new, now)

    async def switch(self, new: int | None, now: float) -> None:
        if self.run is not None and self.source is not None:
            with contextlib.suppress(Exception):
                await self.run.session.finalize()  # 진행 중 문장을 끊지 않고 확정한 뒤 다음 기기를 이어 넣는다
            self.run.next_at = None
        self.source = new
        self.no_source_since = None if new is not None else now
        await self.set_recording("RECORDING" if new is not None else "INTERRUPTED")

    async def set_recording(self, status: RecordingState) -> None:
        self.recording = status
        if self.notify is not None:
            await self.notify()
            return
        await manager.broadcast(
            self.meeting_id,
            "recording.status",
            RecordingStatusChanged(status=status, source_participant_id=self.source),
        )

    async def send(self, pcm: bytes, at: int, now: float) -> None:
        if self.run is None:
            if now < self.retry_at:
                return
            try:
                session = await open_session()
            except Exception:
                logger.exception("리턴제로 세션을 열지 못했습니다: %s", self.meeting_id)
                self.failed, self.retry_at = True, now + RETRY_SECONDS
                return
            self.runs += 1
            self.run = Run(self.runs, session)
            self.failed, self.last_result = False, now
            spawn(self.read(self.run))
        run = self.run
        if run.next_at != at:
            run.anchors.append((run.sent // BYTES_PER_MS, at // BYTES_PER_MS))
        try:
            await run.session.send(pcm)
        except Exception:
            logger.warning("리턴제로로 보내지 못했습니다. 다음 소리에 다시 엽니다: %s", self.meeting_id, exc_info=True)
            await self.stop_run()
            self.failed = True
            return
        run.sent += len(pcm)
        run.next_at = at + len(pcm)

    async def stop_run(self) -> None:
        """EOS 로 닫는다. 남은 결과는 read() 가 엔진이 닫을 때까지 받는다."""
        run, self.run = self.run, None
        if run is not None:
            with contextlib.suppress(Exception):
                await run.session.close()

    async def read(self, run: Run) -> None:
        try:
            async for message in run.session.results():
                self.last_result = clock()
                await self.deliver(run, message)
        except Exception:
            logger.warning("리턴제로 결과를 받지 못했습니다: %s", self.meeting_id, exc_info=True)
        finally:
            if self.run is run:  # 닫지 않았는데 엔진이 끊었다 → 다음 소리에 다시 연다
                self.run, self.failed = None, True

    async def deliver(self, run: Run, message: dict[str, Any]) -> None:
        alternatives = message.get("alternatives") or [{}]
        text = str(alternatives[0].get("text") or "").strip()
        if not text:
            return
        speaker_id, speaker_name = self.speaker or (None, None)
        key = f"{run.number}-{message['seq']}"
        if speaker_id is not None:  # 비대면은 기기마다 세션이 있어 참가자로 구분한다
            key = f"{speaker_id}-{key}"
        offset = run.offset_ms(int(message["start_at"]))
        if not message.get("final"):
            partial = TranscriptPartial(
                segment_key=key, text=text, offset_ms=offset, participant_id=speaker_id, speaker_name=speaker_name
            )
            await manager.broadcast(self.meeting_id, "transcript.partial", partial)
            return

        async with self.sessions() as session:
            agenda = await timer.current_agenda(session, self.meeting_id)
            if agenda is not None:  # 종료 직후 확정된 문장은 마지막 안건에 넣는다
                self.agenda_id = agenda.id
            utterance = Utterance(
                meeting_id=self.meeting_id,
                agenda_id=self.agenda_id,
                participant_id=speaker_id,
                source=UtteranceSource.REALTIME.value,
                speaker_label=speaker_name,
                text=text,
                offset_ms=offset,
                duration_ms=int(message.get("duration") or 0),
            )
            session.add(utterance)
            await session.flush()
            utterance_id = utterance.id
            await session.commit()
        self.last_final = clock()
        final = TranscriptFinal(
            segment_key=key,
            utterance_id=utterance_id,
            text=text,
            offset_ms=offset,
            agenda_id=self.agenda_id,
            participant_id=speaker_id,
            speaker_name=speaker_name,
        )
        await manager.broadcast(self.meeting_id, "transcript.final", final)

    async def update_stt(self, now: float) -> None:
        source = self.devices.get(self.source) if self.source is not None else None
        loud = source is not None and source.volume(now, DELAYED_SECONDS) >= QUIET_RMS
        state: SttState = "OK"
        if source is not None and (self.failed or (loud and now - self.last_result > DELAYED_SECONDS)):
            state = "DELAYED"
        elif now - self.last_final > NO_AUDIO_SECONDS and (source is None or source.volume(now) < QUIET_RMS):
            state = "NO_AUDIO"
        if state != self.stt:
            self.stt = state
            if self.notify is not None:
                await self.notify()
                return
            await manager.broadcast(self.meeting_id, "stt.status", SttStatusChanged(state=state))


class Remote:
    """비대면(REMOTE) 회의(#22). 기준 기기를 고르지 않고 소리를 보내는 기기마다 리턴제로 세션(Live)을 따로 연다.

    세션 규칙(일시 정지, 그 기기 소리 없음 50초, 종료)은 기기마다 따로다. 회의 전체 상태는 기기 상태를 모아 정한다.
    - recording: 한 기기라도 RECORDING 이면 RECORDING, 모두 PAUSED 면 PAUSED, 아니면 INTERRUPTED.
      sourceParticipantId 는 null(기준 기기 없음)
    - sttStatus: 마이크가 켜진 기기 중 하나라도 OK 면 OK, 아니면 DELAYED, 그다음 NO_AUDIO
    """

    source: int | None = None

    def __init__(self, meeting_id: str, sessions: SessionFactory) -> None:
        self.meeting_id = meeting_id
        self.sessions = sessions
        self.devices: dict[int, Live] = {}
        self.recording: RecordingState = "INTERRUPTED"
        self.stt: SttState = "OK"

    async def feed(self, participant_id: int, name: str, pcm: bytes, at: int) -> None:
        device = self.devices.get(participant_id)
        if device is None:
            device = Live(self.meeting_id, self.sessions, (participant_id, name), self.refresh)
            self.devices[participant_id] = device
        await device.feed(participant_id, False, pcm, at)

    async def leave(self, participant_id: int) -> None:
        if (device := self.devices.get(participant_id)) is not None:
            await device.leave(participant_id)

    async def pause(self) -> None:
        for device in list(self.devices.values()):
            await device.pause()

    async def end(self) -> None:
        for device in list(self.devices.values()):
            await device.end()

    async def tick(self) -> None:
        for device in list(self.devices.values()):
            await device.tick()

    async def refresh(self) -> None:
        states = {d.recording for d in self.devices.values()}
        recording: RecordingState = "INTERRUPTED"
        if "RECORDING" in states:
            recording = "RECORDING"
        elif states == {"PAUSED"}:
            recording = "PAUSED"
        if recording != self.recording:
            self.recording = recording
            changed = RecordingStatusChanged(status=recording, source_participant_id=None)
            await manager.broadcast(self.meeting_id, "recording.status", changed)

        mic_on = {d.stt for p, d in self.devices.items() if manager.mic_state(self.meeting_id, p) == MicState.ON}
        stt: SttState = "OK"
        if mic_on and "OK" not in mic_on:
            stt = "DELAYED" if "DELAYED" in mic_on else "NO_AUDIO"
        if stt != self.stt:
            self.stt = stt
            await manager.broadcast(self.meeting_id, "stt.status", SttStatusChanged(state=stt))


# 회의 코드 → 실시간 자막 상태
lives: dict[str, Live | Remote] = {}


async def feed(
    sessions: SessionFactory,
    meeting_id: str,
    participant_id: int,
    is_host: bool,
    pcm: bytes,
    at: int,
    remote_name: str | None = None,
) -> None:
    """실시간으로 온 청크. 비대면 회의면 remote_name 에 그 참가자 이름을 넘긴다."""
    live = lives.get(meeting_id)
    if live is None:
        live = lives[meeting_id] = Live(meeting_id, sessions) if remote_name is None else Remote(meeting_id, sessions)
    if isinstance(live, Remote):
        await live.feed(participant_id, remote_name or "", pcm, at)
    else:
        await live.feed(participant_id, is_host, pcm, at)


async def leave(meeting_id: str, participant_id: int) -> None:
    if (live := lives.get(meeting_id)) is not None:
        await live.leave(participant_id)


async def pause(meeting_id: str) -> None:
    if (live := lives.get(meeting_id)) is not None:
        await live.pause()


async def end(meeting_id: str) -> None:
    if (live := lives.pop(meeting_id, None)) is not None:
        await live.end()


def status(meeting_id: str) -> tuple[SttState, RecordingState, int | None] | None:
    """API-04 용. 아직 소리가 온 적 없으면 None."""
    live = lives.get(meeting_id)
    return None if live is None else (live.stt, live.recording, live.source)
