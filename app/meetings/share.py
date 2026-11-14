"""API-15 공유 링크 발급. POST /api/v1/meetings/{meetingId}/share (참가자)

결과 화면 [링크로 공유하기]. 링크를 가진 사람에게 요약·전체 기록·녹음이 공개된다.
- 끝난 회의만. 아니면 409 INVALID_STATE
- 아직 쓸 수 있는 링크가 있으면 그것을, 없거나 만료됐으면 새로 만든다(지금 + 14일)
- 회의 행을 잠그고 처리해 동시에 눌러도 유효한 링크는 하나다
"""

import secrets
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.auth import CurrentCaller
from app.api.errors import ApiError, ErrorCode
from app.api.schemas import CamelModel, Envelope, KstDatetime, ok
from app.config import get_settings
from app.db.models import MeetingStatus, ShareLink
from app.db.session import get_db
from app.meetings import timer

router = APIRouter()

KEEP = timedelta(days=14)
FAILED_MESSAGE = "링크를 만들지 못했어요. 잠시 후 다시 시도해주세요."


class ShareOut(CamelModel):
    share_id: str
    share_url: str
    expires_at: KstDatetime


def share_url(share_id: str) -> str:
    return f"{get_settings().frontend_base_url.rstrip('/')}/s/{share_id}"


def share_out(link: ShareLink) -> ShareOut:
    return ShareOut(share_id=link.share_id, share_url=share_url(link.share_id), expires_at=link.expires_at)


async def valid_link(session: AsyncSession, meeting_id: str, now: datetime) -> ShareLink | None:
    return await session.scalar(
        select(ShareLink)
        .where(ShareLink.meeting_id == meeting_id, ShareLink.expires_at > now)
        .order_by(ShareLink.expires_at.desc())
        .limit(1)
    )


@router.post(
    "/meetings/{meeting_id}/share",
    summary="API-15 공유 링크 발급",
    description=(
        "결과 화면 [링크로 공유하기]. 끝난 회의에서만 됩니다. 아직 쓸 수 있는 링크가 있으면 그 링크를, "
        "없거나 만료됐으면 새 링크(14일)를 줍니다. 링크를 가진 사람은 요약·전체 기록·녹음을 볼 수 있습니다."
    ),
    responses=docs.success(docs.SHARE) | docs.errors(ErrorCode.UNAUTHORIZED, ErrorCode.INVALID_STATE),
)
async def create_share(
    meeting_id: str, caller: CurrentCaller, session: Annotated[AsyncSession, Depends(get_db)]
) -> Envelope[ShareOut]:
    meeting = await timer.lock_meeting(session, meeting_id)
    if meeting.status != MeetingStatus.ENDED:
        raise ApiError(ErrorCode.INVALID_STATE)
    now = timer.utcnow()
    link = await valid_link(session, meeting_id, now)
    if link is None:
        link = ShareLink(
            meeting_id=meeting_id,
            share_id=secrets.token_urlsafe(16),
            created_by=caller.participant_id,
            expires_at=now + KEEP,
        )
        session.add(link)
    out = share_out(link)
    try:
        await session.commit()
    except SQLAlchemyError as exc:
        raise ApiError(ErrorCode.INTERNAL_ERROR, FAILED_MESSAGE) from exc
    return ok(out)
