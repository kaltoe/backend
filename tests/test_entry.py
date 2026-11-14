from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.rate_limit import LIMIT, entry_limiter
from app.api.schemas import to_kst_iso
from app.db.models import Meeting
from app.meetings.timer import utcnow
from app.realtime.manager import manager


async def add_meeting(session: AsyncSession, **overrides: Any) -> Meeting:
    now = utcnow()
    values: dict[str, Any] = {
        "id": "K7Q2MX",
        "title": "마케팅 전략 주간 회의",
        "status": "IN_PROGRESS",
        "planned_total_minutes": 30,
        "paused_total_seconds": 0,
        "consent_agreed_at": now,
        "host_recovery_key_hash": "hash",
        "started_at": now,
        "expires_at": now + timedelta(days=14),
    }
    values.update(overrides)
    meeting = Meeting(**values)
    session.add(meeting)
    await session.flush()
    return meeting


async def test_entry_info_of_running_meeting(client: httpx.AsyncClient, session: AsyncSession) -> None:
    meeting = await add_meeting(session)
    res = await client.get("/api/v1/meetings/K7Q2MX/entry")
    assert res.status_code == 200
    assert res.json() == {
        "success": True,
        "data": {
            "meetingId": "K7Q2MX",
            "title": "마케팅 전략 주간 회의",
            "status": "IN_PROGRESS",
            "connectedCount": 0,
            "expiresAt": to_kst_iso(meeting.expires_at),
        },
        "error": None,
    }


async def test_paused_meeting_is_joinable(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_meeting(session, status="PAUSED", paused_at=utcnow())
    res = await client.get("/api/v1/meetings/K7Q2MX/entry")
    assert res.status_code == 200
    assert res.json()["data"]["status"] == "PAUSED"


async def test_connected_count_reads_websocket_rooms(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_meeting(session)
    monkeypatch.setattr(manager, "count", lambda meeting_id: 3 if meeting_id == "K7Q2MX" else 0)
    res = await client.get("/api/v1/meetings/K7Q2MX/entry")
    assert res.json()["data"]["connectedCount"] == 3


@pytest.mark.parametrize(
    ("overrides", "status", "code", "message"),
    [
        ({"status": "ENDED", "ended_at": utcnow(), "ended_by": "HOST"}, 410, "MEETING_ENDED", "이미 끝난 회의예요."),
        ({"expires_at": utcnow() - timedelta(seconds=1)}, 410, "MEETING_EXPIRED", "만료된 회의 코드예요."),
        # 끝났으면서 만료된 회의는 '끝났음'을 먼저 알린다
        (
            {"status": "ENDED", "ended_at": utcnow(), "ended_by": "AUTO", "expires_at": utcnow() - timedelta(days=1)},
            410,
            "MEETING_ENDED",
            "이미 끝난 회의예요.",
        ),
    ],
)
async def test_not_joinable(
    client: httpx.AsyncClient,
    session: AsyncSession,
    overrides: dict[str, Any],
    status: int,
    code: str,
    message: str,
) -> None:
    await add_meeting(session, **overrides)
    res = await client.get("/api/v1/meetings/K7Q2MX/entry")
    assert res.status_code == status
    assert res.json() == {"success": False, "data": None, "error": {"code": code, "message": message}}


@pytest.mark.parametrize("meeting_id", ["ZZZZZZ", "k7q2mx", "K7Q2M", "K7Q2MX1", "K0Q2MX"])
async def test_unknown_code_is_404(client: httpx.AsyncClient, session: AsyncSession, meeting_id: str) -> None:
    await add_meeting(session)
    res = await client.get(f"/api/v1/meetings/{meeting_id}/entry")
    assert res.status_code == 404
    assert res.json()["error"] == {"code": "MEETING_NOT_FOUND", "message": "존재하지 않는 회의예요."}


async def test_121st_request_in_a_minute_is_429(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await add_meeting(session)
    for _ in range(LIMIT):
        assert (await client.get("/api/v1/meetings/K7Q2MX/entry")).status_code == 200
    res = await client.get("/api/v1/meetings/K7Q2MX/entry")
    assert res.status_code == 429
    assert res.json()["error"] == {"code": "TOO_MANY_REQUESTS", "message": "잠시 후 다시 시도해주세요."}


async def test_shares_counter_with_sign_in(client: httpx.AsyncClient, session: AsyncSession) -> None:
    # API-03 이 같은 entry_limiter 를 쓰므로, 그쪽에서 120회를 채우면 여기서도 막힌다
    await add_meeting(session)
    for _ in range(LIMIT):
        entry_limiter.hit("127.0.0.1")
    assert (await client.get("/api/v1/meetings/K7Q2MX/entry")).status_code == 429
