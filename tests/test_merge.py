"""합본(#25). 테스트 오디오는 여기서 만든 합성음(사인파)만 쓴다."""

import logging
import shutil
import subprocess
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import httpx
import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Meeting
from app.db.session import SessionFactory
from app.meetings import merge, postprocess
from app.realtime.audio import BYTES_PER_MS, write_pcm
from tests.test_agendas import T0, THREE_AGENDAS, at, bearer, create, start_at_t0

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 가 없습니다")
MS = merge.SAMPLE_RATE // 1000  # 1ms 의 샘플 수


def tone(ms: int, amplitude: int, hz: int = 440) -> np.ndarray:
    t = np.arange(ms * MS) / merge.SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * hz * t)).astype(np.int16)


@pytest.fixture(autouse=True)
def _no_clova(monkeypatch: pytest.MonkeyPatch) -> None:
    """합본 테스트는 CLOVA 요청(#26)까지 가지 않는다."""

    async def skip(sessions: SessionFactory, meeting_id: str) -> None:
        pass

    monkeypatch.setattr(postprocess, "request", skip)


@pytest.fixture
def recordings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "recordings_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def sessions(session: AsyncSession) -> SessionFactory:
    @asynccontextmanager
    async def borrowed() -> AsyncIterator[AsyncSession]:
        yield session

    return borrowed


class Uploaded(NamedTuple):
    key: str
    copy: Path


@pytest.fixture
def uploads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Uploaded]:
    """S3 대신 올린 파일을 복사해 둔다."""
    done: list[Uploaded] = []

    def fake_upload(path: Path, key: str) -> None:
        copy = tmp_path / f"uploaded-{len(done)}.m4a"
        shutil.copy(path, copy)
        done.append(Uploaded(key, copy))

    monkeypatch.setattr(merge, "upload", fake_upload)
    return done


def put(recordings: Path, code: str, pid: int, at_ms: int, pcm: np.ndarray) -> None:
    """WS-02 가 쓰는 것과 같은 방식으로 녹음 위치에 쓴다."""
    write_pcm(recordings / code / f"{pid}.wav", at_ms * BYTES_PER_MS, pcm.astype("<i2").tobytes())
    (recordings / code / f"{pid}.json").write_text("[]")


def decode(path: Path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        check=True,
        capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype="<i2")


def duration_ms(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return float(out) * 1000


async def ended_meeting(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> str:
    """T0 에 시작해 1초 일시 정지 후 T0 + 4초에 끝난 회의. 타임라인은 3000ms."""
    created = (await client.post("/api/v1/meetings", json=body)).json()["data"]
    code: str = created["meetingId"]
    await start_at_t0(session, code)
    headers = bearer(created["accessToken"])
    at(monkeypatch, 1)
    await client.post(f"/api/v1/meetings/{code}/pause", headers=headers)
    at(monkeypatch, 2)
    await client.post(f"/api/v1/meetings/{code}/resume", headers=headers)
    at(monkeypatch, 4)
    await client.post(f"/api/v1/meetings/{code}/end", headers=headers)
    return code


async def reload(session: AsyncSession, code: str) -> Meeting:
    meeting = await session.get(Meeting, code)
    assert meeting is not None
    await session.refresh(meeting)
    return meeting


def test_in_person_picks_loudest_device_per_block() -> None:
    quiet, loud = tone(200, 1000), tone(200, 8000)
    a = np.concatenate([loud[: 100 * MS], quiet[: 100 * MS]])  # 처음 100ms 는 A 가 크고
    b = np.concatenate([quiet[: 100 * MS], loud[: 100 * MS]])  # 다음 100ms 는 B 가 크다
    merged = merge.build([a, b], 300 * MS, mix=False)
    assert len(merged) == 300 * MS  # 회의 타임라인 길이로 채운다
    assert np.array_equal(merged[: 100 * MS], a[: 100 * MS])
    assert np.array_equal(merged[100 * MS : 200 * MS], b[100 * MS :])
    assert not merged[200 * MS :].any()  # 소리가 없던 구간은 무음


def test_remote_mixes_and_clips() -> None:
    a, b = tone(100, 20000), tone(100, 20000)
    merged = merge.build([a, b], 50 * MS, mix=True)
    assert len(merged) == 50 * MS  # 회의보다 길면 자른다
    assert merged.max() == 32767  # 겹친 소리가 넘치면 잘라 맞춘다
    expected = np.clip(a[: 50 * MS].astype(np.int32) * 2, -32768, 32767).astype(np.int16)
    assert np.array_equal(merged, expected)
    assert not merge.build([], 10 * MS, mix=True).any()


@needs_ffmpeg
async def test_in_person_merge_uploads_m4a_and_keeps_flac(
    client: httpx.AsyncClient,
    session: AsyncSession,
    sessions: SessionFactory,
    recordings: Path,
    uploads: list[Uploaded],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = await ended_meeting(client, session, monkeypatch, THREE_AGENDAS)
    put(recordings, code, 1, 0, tone(1000, 8000))  # 진행자 기기: 0~1초
    put(recordings, code, 2, 2000, tone(500, 6000, hz=660))  # 참가자 기기: 2~2.5초(늦게 입장)

    await merge.run(sessions, code)

    meeting = await reload(session, code)
    assert (meeting.recording_status, meeting.recording_m4a_s3_key, meeting.recording_duration_ms) == (
        "READY",
        f"recordings/{code}/merged.m4a",
        3000,  # 일시 정지 1초 제외, 무음 포함
    )
    assert [u.key for u in uploads] == [f"recordings/{code}/merged.m4a"]
    m4a = uploads[0].copy.read_bytes()
    assert m4a.index(b"moov") < m4a.index(b"mdat")  # faststart: 재생 정보가 앞에 있어 중간부터 재생된다
    assert abs(duration_ms(uploads[0].copy) - 3000) < 100

    # 무손실 flac 으로 위치 확인: 0ms 가 회의 시작, 빈 구간은 무음
    pcm = decode(recordings / code / "merged.flac")
    assert len(pcm) == 3000 * MS
    assert np.array_equal(pcm[: 1000 * MS], tone(1000, 8000))
    assert not pcm[1000 * MS : 2000 * MS].any()
    assert np.array_equal(pcm[2000 * MS : 2500 * MS], tone(500, 6000, hz=660))

    # 올린 뒤 기기별 WAV·json 은 지우고 CLOVA 용 flac 만 남긴다
    assert sorted(p.name for p in (recordings / code).iterdir()) == ["merged.flac"]


@needs_ffmpeg
async def test_remote_merge_mixes_without_flac(
    client: httpx.AsyncClient,
    session: AsyncSession,
    sessions: SessionFactory,
    recordings: Path,
    uploads: list[Uploaded],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = await ended_meeting(client, session, monkeypatch, THREE_AGENDAS | {"meetingMode": "REMOTE"})
    put(recordings, code, 1, 0, tone(1000, 4000))
    put(recordings, code, 2, 500, tone(1000, 4000, hz=660))  # 0.5~1초는 둘이 겹친다
    await merge.run(sessions, code)

    assert (await reload(session, code)).recording_status == "READY"
    assert list((recordings / code).iterdir()) == []  # 비대면은 CLOVA 를 쓰지 않아 flac 도 없다
    pcm = decode(uploads[0].copy)
    assert abs(len(pcm) - 3000 * MS) < 100 * MS
    assert np.abs(pcm[700 * MS : 800 * MS].astype(np.int32)).max() > 5000  # 겹친 구간은 둘이 섞여 더 크다


async def test_fails_after_three_attempts_and_keeps_files(
    client: httpx.AsyncClient,
    session: AsyncSession,
    sessions: SessionFactory,
    recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = await ended_meeting(client, session, monkeypatch, THREE_AGENDAS)
    put(recordings, code, 1, 0, tone(100, 1000))
    attempts: list[str] = []

    def broken(folder: Path, meeting_id: str, total_ms: int, remote: bool) -> str:
        attempts.append(meeting_id)
        raise OSError("S3 접속 실패")

    monkeypatch.setattr(merge, "make", broken)
    await merge.run(sessions, code)
    assert len(attempts) == 3
    meeting = await reload(session, code)
    assert (meeting.recording_status, meeting.postprocess_status) == (
        "FAILED",
        "FAILED",
    )  # 합본이 없으면 CLOVA 도 못 함
    assert (recordings / code / "1.wav").exists()  # 실패하면 다시 만들 수 있게 지우지 않는다


async def test_end_starts_merge(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(merge, "start", started.append)
    created = await create(client)
    await client.post(f"/api/v1/meetings/{created['meetingId']}/end", headers=bearer(created["accessToken"]))
    assert started == [created["meetingId"]]


async def test_cleanup_removes_old_files_and_restarts_stuck(
    client: httpx.AsyncClient,
    session: AsyncSession,
    sessions: SessionFactory,
    recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    old, recent, stuck = (await create(client))["meetingId"], (await create(client))["meetingId"], ""
    stuck = (await create(client))["meetingId"]
    for code, hours, status in ((old, 25, "READY"), (recent, 1, "READY"), (stuck, 1, "PROCESSING")):
        meeting = await session.get(Meeting, code)
        assert meeting is not None
        meeting.status, meeting.ended_by, meeting.recording_status = "ENDED", "HOST", status
        meeting.ended_at = T0 - timedelta(hours=hours)
        put(recordings, code, 1, 0, tone(10, 100))
    await session.flush()

    spawned: list[Coroutine[Any, Any, None]] = []
    monkeypatch.setattr(merge, "spawn", spawned.append)
    removed = await merge.cleanup(sessions, T0)
    assert removed == [old]
    assert not (recordings / old).exists() and (recordings / recent).exists()
    assert len(spawned) == 1  # 10분 넘게 PROCESSING 인 회의는 다시 만든다
    spawned[0].close()

    # 디스크 남은 공간이 2GB 아래면 경고
    monkeypatch.setattr(merge.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with caplog.at_level(logging.WARNING):
        await merge.cleanup(sessions, T0)
    assert any("2GB" in r.message for r in caplog.records)
    for coro in spawned[1:]:
        coro.close()
