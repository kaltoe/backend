import asyncio
from array import array
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx_ws import aconnect_ws
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Utterance
from app.db.session import SessionFactory
from app.realtime.manager import manager
from app.stt import live
from tests.test_agendas import bearer, create
from tests.test_realtime import room

LOUD = array("h", [1000] * 4000).tobytes()  # 250ms
LOUDER = array("h", [2000] * 4000).tobytes()
QUIET = bytes(len(LOUD))
MS = 32  # 바이트


class FakeSession:
    def __init__(self) -> None:
        self.sent: list[Any] = []  # 바이트 길이 또는 "FINALIZE"·"EOS"
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def send(self, pcm: bytes) -> None:
        self.sent.append(len(pcm))

    async def finalize(self) -> None:
        self.sent.append("FINALIZE")

    async def close(self) -> None:
        self.sent.append("EOS")
        self.queue.put_nowait(None)  # 엔진이 남은 결과를 보내고 닫는다

    async def results(self) -> AsyncIterator[dict[str, Any]]:
        while (message := await self.queue.get()) is not None:
            yield message


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(live, "clock", c)
    monkeypatch.setattr(live, "TICK_SECONDS", 3600.0)  # 검사는 테스트가 tick() 으로 직접 부른다
    return c


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[FakeSession]:
    sessions: list[FakeSession] = []

    async def fake_open() -> FakeSession:
        sessions.append(FakeSession())
        return sessions[-1]

    monkeypatch.setattr(live, "open_session", fake_open)
    return sessions


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    sent: list[tuple[str, Any]] = []

    async def capture(meeting_id: str, type_: str, data: Any = None) -> None:
        sent.append((type_, data))

    monkeypatch.setattr(manager, "broadcast", capture)
    return sent


@pytest.fixture(autouse=True)
async def _forget_lives() -> AsyncIterator[None]:
    yield
    for each in live.lives.values():
        each.ticker.cancel()
    live.lives.clear()


@pytest.fixture
def sessions(session: AsyncSession) -> SessionFactory:
    @asynccontextmanager
    async def borrowed() -> AsyncIterator[AsyncSession]:
        yield session

    return borrowed


def of(events: list[tuple[str, Any]], type_: str) -> list[Any]:
    return [data for t, data in events if t == type_]


def sources(events: list[tuple[str, Any]]) -> list[tuple[str, int | None]]:
    return [(e.status, e.source_participant_id) for e in of(events, "recording.status")]


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("시간 안에 처리되지 않았습니다")


HOST, A, B = 1, 2, 3


async def test_reference_stream_rules(
    clock: Clock, opened: list[FakeSession], events: list[tuple[str, Any]], sessions: SessionFactory
) -> None:
    code = "TESTAA"

    async def frame(pid: int, pcm: bytes = LOUD) -> None:
        await live.feed(sessions, code, pid, pid == HOST, pcm, 0)

    # 진행자가 없으면 먼저 소리가 온 기기
    await frame(A)
    await frame(B, LOUDER)
    assert sources(events) == [("RECORDING", A)]

    # 대체끼리는 음량이 더 커도 바꾸지 않는다. 진행자 프레임이 3초 이어지면 진행자로
    for t in range(1, 5):
        clock.now = 1000 + t
        await frame(A)
        await frame(B, LOUDER)
        await frame(HOST)
        assert sources(events)[-1] == (("RECORDING", HOST) if t == 4 else ("RECORDING", A))
    session = opened[0]
    assert session.sent[-2:] == ["FINALIZE", len(LOUD)]  # 진행 중 문장을 확정한 뒤 진행자 소리를 이어 넣는다
    assert len(opened) == 1  # 같은 세션

    # 진행자 3초 무음 → 대체 중 최근 10초 음량이 가장 큰 기기
    for t in (5, 6, 7.5):
        clock.now = 1000 + t
        await frame(A)
        await frame(B, LOUDER)
    await live.lives[code].tick()
    assert sources(events)[-1] == ("RECORDING", B)

    # 지금 대체가 빠질 때만 다른 대체로
    clock.now = 1008
    await frame(A, LOUDER)
    await live.leave(code, B)
    assert sources(events)[-1] == ("RECORDING", A)

    # 마이크를 끄면 후보에서 빠진다 → 후보 없음
    manager.set_mic_state(code, A, live.MicState.OFF)
    await live.lives[code].tick()
    assert sources(events)[-1] == ("INTERRUPTED", None)
    clock.now = 1008 + 50
    await live.lives[code].tick()
    assert session.sent[-1] == "FINALIZE"  # 50초까지는 세션을 둔다
    clock.now = 1008 + 50.5
    await live.lives[code].tick()
    assert session.sent[-1] == "EOS"

    # 후보가 다시 생기면 새 세션
    await frame(HOST)
    assert len(opened) == 2
    assert sources(events)[-1] == ("RECORDING", HOST)


async def test_finals_saved_then_sent_with_timeline_offset(
    client: httpx.AsyncClient,
    session: AsyncSession,
    sessions: SessionFactory,
    clock: Clock,
    opened: list[FakeSession],
    events: list[tuple[str, Any]],
) -> None:
    created = await create(client)
    code, first_agenda = created["meetingId"], created["agendas"][0]["agendaId"]

    await live.feed(sessions, code, HOST, True, LOUD, 10_000 * MS)  # 녹음 위치 10초부터
    await live.feed(sessions, code, HOST, True, LOUD, 10_250 * MS)  # 이어짐
    await live.feed(sessions, code, HOST, True, LOUD, 20_000 * MS)  # 끊겼다가 20초
    engine = opened[0]
    assert engine.sent == [len(LOUD)] * 3

    engine.queue.put_nowait(
        {"seq": 0, "start_at": 100, "duration": 0, "final": False, "alternatives": [{"text": "안"}]}
    )
    await until(lambda: bool(of(events, "transcript.partial")))
    partial = of(events, "transcript.partial")[0]
    assert (partial.segment_key, partial.text, partial.offset_ms) == ("1-0", "안", 10_100)

    engine.queue.put_nowait(
        {"seq": 0, "start_at": 100, "duration": 300, "final": True, "alternatives": [{"text": "안녕하세요"}]}
    )
    engine.queue.put_nowait(
        {"seq": 1, "start_at": 520, "duration": 200, "final": True, "alternatives": [{"text": "네"}]}
    )
    await until(lambda: len(of(events, "transcript.final")) == 2)
    finals = of(events, "transcript.final")
    # 500ms 를 넣은 뒤 20초로 건너뛰었으니 세션 520ms → 녹음 20020ms
    assert [(f.segment_key, f.offset_ms, f.agenda_id) for f in finals] == [
        ("1-0", 10_100, first_agenda),
        ("1-1", 20_020, first_agenda),
    ]
    saved = await session.get(Utterance, finals[0].utterance_id)
    assert saved is not None  # 저장한 뒤에 보낸다
    assert (saved.source, saved.text, saved.offset_ms, saved.duration_ms) == ("REALTIME", "안녕하세요", 10_100, 300)
    assert (saved.participant_id, saved.speaker_label) == (None, None)

    # 일시 정지하면 EOS, 재개 후 첫 소리에 새 세션. 시각은 새 세션 기준점으로 바꾼다
    await live.pause(code)
    assert engine.sent[-1] == "EOS"
    assert sources(events)[-1] == ("PAUSED", None)
    await live.feed(sessions, code, HOST, True, LOUD, 30_000 * MS)
    assert len(opened) == 2
    opened[1].queue.put_nowait(
        {"seq": 0, "start_at": 50, "duration": 100, "final": True, "alternatives": [{"text": "다시"}]}
    )
    await until(lambda: len(of(events, "transcript.final")) == 3)
    again = of(events, "transcript.final")[2]
    assert (again.segment_key, again.offset_ms) == ("2-0", 30_050)

    # 종료하면 EOS
    await live.end(code)
    assert opened[1].sent[-1] == "EOS"
    assert code not in live.lives


async def test_stt_status(
    clock: Clock, opened: list[FakeSession], events: list[tuple[str, Any]], sessions: SessionFactory
) -> None:
    code = "TESTBB"
    for t in range(7):  # 소리는 오는데 결과가 5초 넘게 없다
        clock.now = 1000 + t
        await live.feed(sessions, code, HOST, True, LOUD, t * 1000 * MS)
    await live.lives[code].tick()
    assert [e.state for e in of(events, "stt.status")] == ["DELAYED"]

    opened[0].queue.put_nowait(
        {"seq": 0, "start_at": 0, "duration": 0, "final": False, "alternatives": [{"text": "음"}]}
    )
    await until(lambda: live.lives[code].last_result == clock.now)
    await live.lives[code].tick()
    assert of(events, "stt.status")[-1].state == "OK"

    for t in range(7, 32):  # 30초 동안 확정 발화가 없고 조용하다
        clock.now = 1000 + t
        await live.feed(sessions, code, HOST, True, QUIET, t * 1000 * MS)
    await live.lives[code].tick()
    assert of(events, "stt.status")[-1].state == "NO_AUDIO"


async def test_open_failure_is_delayed_and_retried(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, events: list[tuple[str, Any]], sessions: SessionFactory
) -> None:
    calls = 0

    async def broken() -> FakeSession:
        nonlocal calls
        calls += 1
        raise ConnectionError("리턴제로 접속 실패")

    monkeypatch.setattr(live, "open_session", broken)
    code = "TESTCC"
    await live.feed(sessions, code, HOST, True, LOUD, 0)
    await live.feed(sessions, code, HOST, True, LOUD, 250 * MS)
    assert calls == 1  # 5초 동안은 다시 열지 않는다
    await live.lives[code].tick()
    assert of(events, "stt.status")[-1].state == "DELAYED"
    clock.now += 5
    await live.feed(sessions, code, HOST, True, LOUD, 500 * MS)
    assert calls == 2


async def test_audio_channel_feeds_in_person_only(
    app: FastAPI, clock: Clock, opened: list[FakeSession], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "recordings_dir", str(tmp_path))
    start = {"type": "audio.start", "sampleRate": 16000, "encoding": "LINEAR16", "channels": 1, "startOffsetMs": None}
    async with room(app) as r:
        async with aconnect_ws(
            f"/ws/meetings/{r.code}/audio?token={r.host_token}", r.client, keepalive_ping_interval_seconds=None
        ) as ws:
            await ws.send_json(start)
            await ws.send_bytes(LOUD)
            await until(lambda: bool(opened) and opened[0].sent == [len(LOUD)])
            res = await r.client.get(f"/api/v1/meetings/{r.code}/state", headers=bearer(r.host_token))
            data = res.json()["data"]
            assert data["recording"] == {"status": "RECORDING", "sourceParticipantId": r.host_id}
        await until(lambda: not live.lives[r.code].devices)  # 연결이 끊기면 후보에서 빠진다

        remote = (
            await r.client.post(
                "/api/v1/meetings",
                json={
                    "title": "원격 회의",
                    "consentAgreed": True,
                    "meetingMode": "REMOTE",
                    "agendas": [{"title": "리뷰", "allocatedMinutes": 10}],
                },
            )
        ).json()["data"]
        async with aconnect_ws(
            f"/ws/meetings/{remote['meetingId']}/audio?token={remote['accessToken']}",
            r.client,
            keepalive_ping_interval_seconds=None,
        ) as ws:
            await ws.send_json(start)
            await ws.send_bytes(LOUD)
            await asyncio.sleep(0.2)
        assert remote["meetingId"] not in live.lives  # 비대면은 #22
