"""회의 자동 종료 (API-08 기타). 진행자가 나가 버려도 회의가 끝없이 열려 있지 않게 서버가 끝낸다.

- 조건 1: WS-01 에 접속한 사람이 10분 넘게 아무도 없음
- 조건 2: 벽시계로 started_at + 목표 시간 + 60분이 지남(일시 정지 시간도 포함)
- 종료는 API-08 과 같은 finish() 를 ended_by=AUTO 로 부르고, 응답 없이 meeting.ended 만 보낸다

앱이 뜰 때 sweep_loop 를 띄워 30초마다 검사한다. 비어 있기 시작한 시각은 서버 메모리에만 두므로,
서버가 다시 뜨면 부재 10분은 그때부터 다시 센다(재시작 직후 진행 중 회의가 바로 끝나지 않는다).
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.models import EndedBy, Meeting, MeetingStatus
from app.db.session import SessionFactory, get_session_factory
from app.meetings import timer
from app.meetings.end import announce, finish
from app.realtime.manager import manager

logger = logging.getLogger(__name__)

SWEEP_SECONDS = 30.0
EMPTY_LIMIT = timedelta(minutes=10)
OVERTIME_LIMIT = timedelta(minutes=60)

# 회의 코드 → 접속자가 없다는 것을 처음 본 시각.
# ponytail: 검사 주기(30초)만큼 늦게 끝날 수 있다. 정확히 10분이 필요하면 manager.remove 에서 시각을 남긴다
empty_since: dict[str, datetime] = {}


def due(meeting: Meeting, now: datetime) -> bool:
    """자동 종료할 회의인지. 접속자 부재 시각(empty_since)도 여기서 갱신한다."""
    if now >= meeting.started_at + timedelta(minutes=meeting.planned_total_minutes) + OVERTIME_LIMIT:
        return True
    if manager.count(meeting.id) > 0:
        empty_since.pop(meeting.id, None)
        return False
    return now - empty_since.setdefault(meeting.id, now) > EMPTY_LIMIT


async def sweep(sessions: SessionFactory, now: datetime) -> list[str]:
    """끝나지 않은 회의를 모두 보고 조건에 맞는 회의를 끝낸다. 끝낸 회의 코드를 돌려준다."""
    async with sessions() as session:
        live = list(await session.scalars(select(Meeting).where(Meeting.status != MeetingStatus.ENDED.value)))
    candidates = [m.id for m in live if due(m, now)]
    for gone in set(empty_since) - {m.id for m in live}:  # 다른 경로로 끝난 회의는 잊는다
        empty_since.pop(gone, None)

    ended: list[str] = []
    for meeting_id in candidates:
        async with sessions() as session:
            meeting = await timer.lock_meeting(session, meeting_id)
            if meeting.status == MeetingStatus.ENDED:  # 그 사이 진행자가 끝냈다
                continue
            event = await finish(session, meeting, EndedBy.AUTO, now)
            await session.commit()
        empty_since.pop(meeting_id, None)
        await announce(meeting_id, event)
        ended.append(meeting_id)
    return ended


async def sweep_loop() -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            # 엔진은 첫 검사 때 만든다. 앱 시작 때 만들면 DB 설정이 틀렸을 때 /health 까지 뜨지 않는다
            for meeting_id in await sweep(get_session_factory(), timer.utcnow()):
                logger.info("회의를 자동 종료했습니다: %s", meeting_id)
        except Exception:
            logger.exception("자동 종료 검사 실패")
