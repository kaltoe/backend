"""API-13 녹음 조회. GET /api/v1/meetings/{meetingId}/recording (참가자)

결과 하단 플레이어. READY 면 S3 합본(recordings/{meetingId}/merged.m4a)의 1시간짜리 presigned GET 주소를 준다.
- PROCESSING 이면 url 이 null 이고 프론트는 3초마다 다시 부른다. 재생 중 주소가 만료되면 다시 불러 주소만 바꾼다
- 종료 14일(S3 수명 주기와 같음)이 지나면 S3 를 확인하지 않고 410 RECORDING_EXPIRED
- 합본은 faststart 라 프론트가 재생 위치를 offsetMs / 1000 으로 옮기면 브라우저가 그 구간만 받아 바로 재생한다
presign() 은 공유 녹음(API-18, #32)에서도 쓴다.
"""

import asyncio
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any, Literal

import boto3
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.auth import CurrentCaller
from app.api.errors import ApiError, ErrorCode
from app.api.schemas import CamelModel, Envelope, KstDatetime, ok
from app.config import get_settings
from app.db.models import Meeting, RecordingStatus
from app.db.session import get_db
from app.meetings import timer

router = APIRouter()

URL_LIFETIME = timedelta(hours=1)
KEEP = timedelta(days=14)
MIME: Literal["audio/mp4"] = "audio/mp4"


class RecordingOut(CamelModel):
    status: RecordingStatus
    url: str | None
    url_expires_at: KstDatetime | None
    duration_ms: int | None
    mime_type: Literal["audio/mp4"] | None


@lru_cache
def s3_client() -> Any:
    """클라이언트를 만들 때마다 설정 파일을 읽고 자격 증명을 다시 받으므로 프로세스에 하나만 둔다."""
    return boto3.client("s3", region_name=get_settings().aws_region)


def presign(key: str, now: datetime) -> tuple[str, datetime]:
    """S3 presigned GET 주소와 만료 시각. 서명은 서버에서 하므로 S3 에 묻지 않는다(EC2 인스턴스 역할로 서명).

    인스턴스 역할은 임시 자격 증명이라, 그 자격 증명이 먼저 끝나면 주소도 urlExpiresAt 보다 일찍 끊길 수 있다.
    그래서 프론트는 재생 중 403 이 나도 이 API 를 다시 부른다(노션 API-13).
    """
    settings = get_settings()
    url = s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key, "ResponseContentType": MIME},
        ExpiresIn=int(URL_LIFETIME.total_seconds()),
    )
    return str(url), now + URL_LIFETIME


def expired(meeting: Meeting, now: datetime) -> bool:
    return meeting.ended_at is not None and now >= meeting.ended_at + KEEP


def recording_of(meeting: Meeting, now: datetime) -> RecordingOut:
    """API-13·18 공통 응답. 만료 확인은 부르는 쪽이 먼저 한다."""
    key = meeting.recording_m4a_s3_key
    if meeting.recording_status != RecordingStatus.READY or key is None:
        # 종료 전(recording_status NULL)도 PROCESSING 으로 알린다
        status = (
            RecordingStatus.FAILED if meeting.recording_status == RecordingStatus.FAILED else RecordingStatus.PROCESSING
        )
        return RecordingOut(status=status, url=None, url_expires_at=None, duration_ms=None, mime_type=None)
    status = RecordingStatus.READY
    url, expires_at = presign(key, now)
    return RecordingOut(
        status=status, url=url, url_expires_at=expires_at, duration_ms=meeting.recording_duration_ms, mime_type=MIME
    )


@router.get(
    "/meetings/{meeting_id}/recording",
    summary="API-13 녹음 조회",
    description=(
        "결과 하단 플레이어. `READY`면 `url`(1시간짜리 S3 주소, audio/mp4)을 audio src 에 그대로 씁니다. "
        "`PROCESSING`이면 `url`이 null 이고 3초마다 다시 부릅니다. 재생 중 주소가 만료되면 다시 불러 주소만 바꾸고 "
        "재생 위치는 이어 갑니다. 문장을 눌러 재생할 때는 재생 위치를 `offsetMs / 1000`으로 옮깁니다. "
        "종료 14일이 지나면 410 `RECORDING_EXPIRED`입니다."
    ),
    responses=docs.success(docs.RECORDING) | docs.errors(ErrorCode.UNAUTHORIZED, ErrorCode.RECORDING_EXPIRED),
)
async def get_recording(
    meeting_id: str, caller: CurrentCaller, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[RecordingOut]:
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None:  # 토큰은 이 회의 것이므로 없을 일이 거의 없다(API-04 와 같이 401)
        raise ApiError(ErrorCode.UNAUTHORIZED)
    now = timer.utcnow()
    if expired(meeting, now):
        raise ApiError(ErrorCode.RECORDING_EXPIRED)
    # 자격 증명을 처음 받거나 갱신할 때 인스턴스 메타데이터에 HTTP 로 묻는다. 이벤트 루프를 막지 않게 스레드에서 서명
    return ok(await asyncio.to_thread(recording_of, meeting, now))
