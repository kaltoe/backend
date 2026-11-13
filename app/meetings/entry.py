"""API-01 회의 입장 정보 조회. GET /api/v1/meetings/{meetingId}/entry (토큰 불필요)

참여 화면의 제목·상태 배지·인원 배지를 그린다. 끝났거나 만료된 회의는 에러로 돌려준다.
같은 IP 요청은 API-03과 한 카운터로 1분 120회까지.
"""

import re
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import docs
from app.api.errors import ApiError, ErrorCode
from app.api.rate_limit import limit_entry_requests
from app.api.schemas import CamelModel, Envelope, KstDatetime, ok
from app.db.models import Meeting, MeetingStatus
from app.db.session import get_db
from app.meetings.codes import CODE_ALPHABET, MEETING_CODE_LENGTH
from app.meetings.timer import utcnow
from app.realtime.manager import manager

router = APIRouter()

MEETING_CODE = re.compile(f"^[{CODE_ALPHABET}]{{{MEETING_CODE_LENGTH}}}$")


class EntryInfo(CamelModel):
    meeting_id: str
    title: str
    status: Literal["IN_PROGRESS", "PAUSED"]
    connected_count: int
    expires_at: KstDatetime


def ensure_joinable(meeting: Meeting | None, now: datetime) -> Meeting:
    """입장(API-01·API-03)할 수 있는 회의인지 본다. 끝났으면서 만료된 회의는 '끝났음'을 먼저 알린다."""
    if meeting is None:
        raise ApiError(ErrorCode.MEETING_NOT_FOUND)
    if meeting.status == MeetingStatus.ENDED:
        raise ApiError(ErrorCode.MEETING_ENDED)
    if meeting.expires_at <= now:
        raise ApiError(ErrorCode.MEETING_EXPIRED)
    return meeting


async def find_meeting(session: AsyncSession, meeting_id: str) -> Meeting | None:
    # 형식이 틀린 코드는 DB를 보지 않고 없는 회의로 처리한다
    if not MEETING_CODE.match(meeting_id):
        return None
    return await session.get(Meeting, meeting_id)


@router.get(
    "/meetings/{meeting_id}/entry",
    dependencies=[Depends(limit_entry_requests)],
    summary="API-01 회의 입장 정보 조회",
    description=(
        "메인에서 코드나 링크를 넣으면 참여 화면의 제목과 상태 배지를 보여 줍니다. 코드는 대문자 6자리입니다"
        "(프론트가 링크의 마지막 조각을 대문자로 바꿔 보냄). `status`는 진행 중·일시 정지만 오고, "
        "`connectedCount`는 지금 접속 중인 인원입니다. 같은 IP에서 1분에 120번을 넘기면 429입니다."
    ),
    responses=docs.success(docs.ENTRY_INFO)
    | docs.errors(
        ErrorCode.MEETING_NOT_FOUND, ErrorCode.MEETING_EXPIRED, ErrorCode.MEETING_ENDED, ErrorCode.TOO_MANY_REQUESTS
    ),
)
async def get_entry_info(meeting_id: str, session: Annotated[AsyncSession, Depends(get_db)]) -> Envelope[EntryInfo]:
    meeting = ensure_joinable(await find_meeting(session, meeting_id), utcnow())
    return ok(
        EntryInfo(
            meeting_id=meeting.id,
            title=meeting.title,
            status="PAUSED" if meeting.status == MeetingStatus.PAUSED else "IN_PROGRESS",
            connected_count=manager.count(meeting.id),
            expires_at=meeting.expires_at,
        )
    )
