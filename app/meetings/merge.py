"""종료 후 다시 듣기용 합본(#25). 회의가 끝나면 백그라운드로 기기별 WAV(WS-02)를 합쳐 S3 에 올린다.

합본 규칙(API-13)
- 기기별 WAV 는 이미 녹음 위치(회의 타임라인)에 맞춰 쓰여 있고 빈 구간은 무음이다. 그래서 길이만 회의 타임라인
  (일시 정지 제외)에 맞추면 합본의 0ms 가 회의 시작이고 전문 offsetMs 와 위치가 그대로 맞는다
- 비대면: 기기별 소리를 겹쳐 섞는다(믹스)
- 대면: 한 방의 기기들이 같은 소리를 받으므로 100ms 구간마다 음량이 가장 큰 기기 하나를 쓴다(2026-11 결정)
- merged.m4a(AAC, faststart)를 recordings/{meetingId}/merged.m4a 로 올린다. 대면은 CLOVA 용 merged.flac 도 만든다(#26)
- 성공하면 recording_status READY, 기기별 WAV·json 을 지운다. 만들기·업로드를 3번 해도 안 되면 FAILED

정리: 1시간마다 종료 후 24시간이 지난 회의의 남은 파일을 지우고, 디스크 남은 공간이 2GB 아래면 경고한다.
"""

import asyncio
import logging
import shutil
import subprocess
import wave
from datetime import datetime, timedelta
from pathlib import Path

import boto3
import numpy as np
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Meeting, MeetingMode, MeetingStatus, RecordingStatus
from app.db.session import SessionFactory, get_session_factory
from app.meetings import timer
from app.stt.live import spawn

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK = SAMPLE_RATE // 10  # 100ms
CHUNK = BLOCK * 600  # 60초씩 나눠 합친다. 기기 수 × 회의 길이만큼 메모리를 한 번에 잡지 않는다
WAV_HEADER = 44
ATTEMPTS = 3
SETTLE_SECONDS = 2.0  # 종료 직후 음성 연결이 마지막 청크·json 을 쓸 시간
CLEANUP_SECONDS = 3600.0
KEEP_AFTER_END = timedelta(hours=24)
STUCK_AFTER = timedelta(minutes=10)  # 이만큼 지나도 PROCESSING 이면(서버 재시작 등) 다시 만든다
DISK_WARN_BYTES = 2 * 1024**3

running: set[str] = set()


def folder_of(meeting_id: str) -> Path:
    return Path(get_settings().recordings_dir) / meeting_id


def device_wavs(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob("*.wav") if not p.name.startswith("merged"))


def read_pcm(path: Path, start: int = 0, count: int = -1) -> np.ndarray:
    """start 샘플부터 count 개(-1 이면 끝까지). 파일보다 뒤면 빈 배열."""
    available = max(path.stat().st_size - WAV_HEADER, 0) // 2 - start
    if available <= 0 or count == 0:
        return np.zeros(0, dtype="<i2")
    n = available if count < 0 else min(count, available)
    return np.fromfile(path, dtype="<i2", count=n, offset=WAV_HEADER + start * 2)


def build(tracks: list[np.ndarray], total: int, mix: bool) -> np.ndarray:
    """기기별 PCM 을 total 샘플 길이 하나로 만든다. mix 면 겹쳐 섞고, 아니면 구간마다 가장 큰 소리를 고른다."""
    if not tracks:
        return np.zeros(total, dtype=np.int16)
    blocks = -(-total // BLOCK)
    stack = np.zeros((len(tracks), blocks * BLOCK), dtype=np.int16)
    for i, track in enumerate(tracks):
        n = min(len(track), total)
        stack[i, :n] = track[:n]
    if mix:
        merged = np.clip(stack.astype(np.int32).sum(axis=0), -32768, 32767).astype(np.int16)
    else:
        by_block = stack.reshape(len(tracks), blocks, BLOCK)
        loudest = np.sqrt(np.mean(by_block.astype(np.float32) ** 2, axis=2)).argmax(axis=0)
        merged = by_block[loudest, np.arange(blocks)].reshape(-1)
    return merged[:total]


def write_merged(path: Path, wavs: list[Path], total: int, mix: bool) -> None:
    """기기별 WAV 를 CHUNK 구간씩 읽어 build() 하고 이어 쓴다. CHUNK 가 BLOCK 의 배수라 한 번에 만든 것과 같다."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        for start in range(0, total, CHUNK):
            end = min(start + CHUNK, total)
            part = build([read_pcm(p, start, end - start) for p in wavs], end - start, mix)
            w.writeframes(part.astype("<i2").tobytes())


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, capture_output=True)


def upload(path: Path, key: str) -> None:
    """EC2 인스턴스 역할로 인증한다(액세스 키를 두지 않는다)."""
    settings = get_settings()
    client = boto3.client("s3", region_name=settings.aws_region)
    client.upload_file(str(path), settings.s3_bucket, key, ExtraArgs={"ContentType": "audio/mp4"})


def make(folder: Path, meeting_id: str, total_ms: int, remote: bool) -> str:
    """합본을 만들어 올리고 S3 키를 돌려준다. 대면은 merged.flac 을 남긴다."""
    wavs = device_wavs(folder)
    folder.mkdir(parents=True, exist_ok=True)
    source, m4a = folder / "merged.wav", folder / "merged.m4a"
    write_merged(source, wavs, total_ms * SAMPLE_RATE // 1000, mix=remote)
    try:
        ffmpeg("-i", str(source), "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(m4a))
        if not remote:
            ffmpeg("-i", str(source), "-c:a", "flac", str(folder / "merged.flac"))
    finally:
        source.unlink(missing_ok=True)
    key = f"recordings/{meeting_id}/merged.m4a"
    upload(m4a, key)
    m4a.unlink(missing_ok=True)
    return key


def remove_device_files(folder: Path) -> None:
    for path in [*device_wavs(folder), *folder.glob("*.json")]:
        path.unlink(missing_ok=True)


async def run(sessions: SessionFactory, meeting_id: str) -> None:
    """회의 하나의 합본. 같은 회의가 이미 돌고 있으면 넘어간다."""
    if meeting_id in running:
        return
    running.add(meeting_id)
    try:
        async with sessions() as session:
            meeting = await session.get(Meeting, meeting_id)
            if meeting is None or meeting.status != MeetingStatus.ENDED or meeting.ended_at is None:
                return
            total_ms = timer.timeline_ms(meeting, meeting.ended_at)  # 일시 정지 제외, 무음 포함
            remote = meeting.meeting_mode == MeetingMode.REMOTE
        folder = folder_of(meeting_id)
        key: str | None = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                key = await asyncio.to_thread(make, folder, meeting_id, total_ms, remote)
                break
            except Exception:
                logger.exception("합본 만들기·업로드 실패(%d/%d): %s", attempt, ATTEMPTS, meeting_id)
        async with sessions() as session:
            meeting = await session.get(Meeting, meeting_id)
            if meeting is None:
                return
            if key is None:
                meeting.recording_status = RecordingStatus.FAILED.value
            else:
                meeting.recording_status = RecordingStatus.READY.value
                meeting.recording_m4a_s3_key = key
                meeting.recording_duration_ms = total_ms
            await session.commit()
        if key is not None:
            await asyncio.to_thread(remove_device_files, folder)
    finally:
        running.discard(meeting_id)


async def after_end(meeting_id: str) -> None:
    await asyncio.sleep(SETTLE_SECONDS)
    try:
        await run(get_session_factory(), meeting_id)
    except Exception:
        logger.exception("합본 처리 중 오류: %s", meeting_id)


def start(meeting_id: str) -> None:
    """회의 종료(API-08·자동 종료) 직후 부른다."""
    spawn(after_end(meeting_id))


async def cleanup(sessions: SessionFactory, now: datetime) -> list[str]:
    """종료 24시간이 지난 회의의 남은 파일을 지우고, 멈춘 합본을 다시 띄운다. 지운 회의 코드를 돌려준다."""
    root = Path(get_settings().recordings_dir)
    if not root.exists():
        return []
    if shutil.disk_usage(root).free < DISK_WARN_BYTES:
        logger.warning("녹음 디스크 남은 공간이 2GB 아래입니다: %s", root)
    folders = {p.name: p for p in root.iterdir() if p.is_dir()}
    async with sessions() as session:
        meetings = list(await session.scalars(select(Meeting).where(Meeting.id.in_(folders))))
    removed: list[str] = []
    for meeting in meetings:
        if meeting.ended_at is None:
            continue
        if now - meeting.ended_at > KEEP_AFTER_END:
            await asyncio.to_thread(shutil.rmtree, folders[meeting.id], True)
            removed.append(meeting.id)
        elif meeting.recording_status == RecordingStatus.PROCESSING and now - meeting.ended_at > STUCK_AFTER:
            spawn(run(sessions, meeting.id))
    return removed


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_SECONDS)
        try:
            for meeting_id in await cleanup(get_session_factory(), timer.utcnow()):
                logger.info("종료 24시간이 지난 녹음 파일을 지웠습니다: %s", meeting_id)
        except Exception:
            logger.exception("녹음 파일 정리 실패")
