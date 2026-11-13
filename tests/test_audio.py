import asyncio
import json
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx_ws import AsyncWebSocketSession, aconnect_ws
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.realtime import audio
from app.realtime.audio import BYTES_PER_MS, Track
from tests.test_agendas import at, bearer, start_at_t0
from tests.test_realtime import Room, close_code, room

CHUNK = b"\x01\x00" * 4000  # 250ms


@pytest.fixture(autouse=True)
def recordings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(get_settings(), "recordings_dir", str(tmp_path))
    audio.audio_sockets.clear()
    yield tmp_path
    audio.audio_sockets.clear()


def connect(r: Room, token: str | None, code: str | None = None) -> Any:
    query = f"?token={token}" if token is not None else ""
    return aconnect_ws(f"/ws/meetings/{code or r.code}/audio{query}", r.client, keepalive_ping_interval_seconds=None)


def start(offset: int | None = None, **override: Any) -> dict[str, Any]:
    return {
        "type": "audio.start",
        "sampleRate": 16000,
        "encoding": "LINEAR16",
        "channels": 1,
        "startOffsetMs": offset,
    } | override


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("시간 안에 처리되지 않았습니다")


def registered(r: Room, pid: int) -> Callable[[], bool]:
    return lambda: pid in audio.audio_sockets.get(r.code, {})


def frames(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getsampwidth(), w.getnchannels()) == (16000, 2, 1)
        return w.readframes(w.getnframes())


def size_is(path: Path, ms: int) -> Callable[[], bool]:
    return lambda: path.exists() and path.stat().st_size == 44 + ms * BYTES_PER_MS


async def send_chunk(ws: AsyncWebSocketSession, wav: Path, end_ms: int) -> None:
    await ws.send_bytes(CHUNK)
    await until(size_is(wav, end_ms))  # 서버가 쓸 때까지 기다린다(테스트는 DB 세션 하나를 같이 쓴다)


async def test_close_codes(app: FastAPI) -> None:
    async with room(app) as r:
        cases = [
            (None, None, start(), 4401),
            ("not-a-token", None, start(), 4401),
            (r.host_token, "ZZZZZZ", start(), 4404),
            (r.host_token, None, start(sampleRate=44100), 1008),
            (r.host_token, None, {"type": "ping"}, 1008),
            (r.host_token, None, start(offset=10**9), 1008),  # 아직 오지 않은 위치
        ]
        for token, code, first, expected in cases:
            async with connect(r, token, code) as ws:
                await ws.send_json(first)
                assert await close_code(ws) == expected

        await r.client.post(f"/api/v1/meetings/{r.code}/end", headers=bearer(r.host_token))
        async with connect(r, r.host_token) as ws:
            assert await close_code(ws) == 4410


async def test_buffer_older_than_10_minutes_is_refused(
    app: FastAPI, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with room(app) as r:
        await start_at_t0(session, r.code)
        at(monkeypatch, 900)  # 녹음 위치 900초
        async with connect(r, r.host_token) as ws:
            await ws.send_json(start(offset=(900 - 600) * 1000 - 1))
            assert await close_code(ws) == 1008
        async with connect(r, r.host_token) as ws:
            await ws.send_json(start(offset=(900 - 600) * 1000))  # 딱 10분 전은 받는다
            await until(registered(r, r.host_id))


async def test_records_at_timeline_position_and_drops_paused(
    app: FastAPI, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, recordings: Path
) -> None:
    async with room(app) as r:
        await start_at_t0(session, r.code)
        wav = recordings / r.code / f"{r.host_id}.wav"
        async with connect(r, r.host_token) as ws:
            await ws.send_json(start())
            at(monkeypatch, 10)
            await send_chunk(ws, wav, 10_000)  # 9750~10000ms

            at(monkeypatch, 20)
            await r.client.post(f"/api/v1/meetings/{r.code}/pause", headers=bearer(r.host_token))
            at(monkeypatch, 25)
            await ws.send_bytes(CHUNK)  # 일시 정지 중 → 버린다
            await asyncio.sleep(0.2)
            assert size_is(wav, 10_000)()
            at(monkeypatch, 30)
            await r.client.post(f"/api/v1/meetings/{r.code}/resume", headers=bearer(r.host_token))

            at(monkeypatch, 31)  # 녹음 위치 21000ms. 앞 청크와 1초 넘게 벌어져 지금 위치로 맞춘다
            await send_chunk(ws, wav, 21_000)
            at(monkeypatch, 31.25)
            await send_chunk(ws, wav, 21_250)  # 이어 쓴다

        data = frames(wav)
        silence = b"\x00" * (9750 * BYTES_PER_MS)
        gap = b"\x00" * ((20_750 - 10_000) * BYTES_PER_MS)
        assert data == silence + CHUNK + gap + CHUNK + CHUNK
        spans = recordings / r.code / f"{r.host_id}.json"
        await until(spans.exists)
        assert json.loads(spans.read_text()) == [
            {"startMs": 9750, "endMs": 10_000},
            {"startMs": 20_750, "endMs": 21_250},
        ]


async def test_reconnect_buffer_goes_to_wav(
    app: FastAPI, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, recordings: Path
) -> None:
    async with room(app) as r:
        await start_at_t0(session, r.code)
        wav = recordings / r.code / f"{r.host_id}.wav"
        spans = recordings / r.code / f"{r.host_id}.json"
        at(monkeypatch, 10)
        async with connect(r, r.host_token) as ws:
            await ws.send_json(start())
            await send_chunk(ws, wav, 10_000)
        await until(spans.exists)

        at(monkeypatch, 60)  # 끊긴 동안 쌓인 버퍼를 10초 위치부터 다시 보낸다
        async with connect(r, r.host_token) as ws:
            await ws.send_json(start(offset=10_000))
            await send_chunk(ws, wav, 10_250)
            await send_chunk(ws, wav, 10_500)

        assert frames(wav)[9750 * BYTES_PER_MS :] == CHUNK * 3
        merged = [{"startMs": 9750, "endMs": 10_500}]  # 이전 연결 구간과 합친다
        await until(lambda: json.loads(spans.read_text()) == merged)


def test_late_buffer_is_not_live() -> None:
    track = Track(cursor=10_000 * BYTES_PER_MS)  # 재연결 버퍼가 10초 위치부터
    size = len(CHUNK)
    assert track.place(size, live_ms=60_000) == (10_000 * BYTES_PER_MS, True)  # 늦은 소리: WAV 에만
    track.cursor = 59_000 * BYTES_PER_MS
    assert track.place(size, live_ms=60_000)[1] is False  # 따라잡으면 실시간
    # 따라잡은 뒤에 벌어지면(마이크 OFF 등) 지금 위치로 맞춘다
    assert track.place(size, live_ms=70_000) == ((70_000 - 250) * BYTES_PER_MS, False)


async def test_new_device_closes_previous_with_4409(app: FastAPI) -> None:
    async with room(app) as r, connect(r, r.host_token) as first:
        await first.send_json(start())
        await until(registered(r, r.host_id))
        previous = audio.audio_sockets[r.code][r.host_id]
        async with connect(r, r.host_token) as second:
            await second.send_json(start())
            assert await close_code(first) == 4409
            await until(lambda: audio.audio_sockets[r.code].get(r.host_id) not in (None, previous))


async def test_meeting_end_closes_audio_with_4410(app: FastAPI) -> None:
    async with room(app) as r:
        member_id, member_token = await r.member("민지")
        async with connect(r, member_token) as ws:
            await ws.send_json(start())
            await until(registered(r, member_id))
            await r.client.post(f"/api/v1/meetings/{r.code}/end", headers=bearer(r.host_token))
            assert await close_code(ws) == 4410
        assert r.code not in audio.audio_sockets
