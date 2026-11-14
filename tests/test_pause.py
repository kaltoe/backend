from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import to_kst_iso
from app.db.models import Meeting
from app.meetings import timer
from tests.test_realtime import CREATE, ready, receive, room

T0 = timer.utcnow().replace(microsecond=0) - timedelta(hours=1)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def create(client: httpx.AsyncClient) -> tuple[str, str]:
    data = (await client.post("/api/v1/meetings", json=CREATE)).json()["data"]
    return data["meetingId"], data["accessToken"]


async def test_pause_then_resume_keeps_timeline(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, token = await create(client)
    meeting = await session.get(Meeting, code)
    assert meeting is not None
    meeting.started_at = T0
    await session.flush()

    monkeypatch.setattr(timer, "utcnow", lambda: T0 + timedelta(seconds=60))
    res = await client.post(f"/api/v1/meetings/{code}/pause", headers=bearer(token))
    assert res.status_code == 200
    paused = res.json()["data"]
    assert paused["pausedAt"] == to_kst_iso(T0 + timedelta(seconds=60))
    assert paused["timer"]["status"] == "PAUSED"
    assert paused["timer"]["totalElapsedSeconds"] == 60
    await session.refresh(meeting)  # API 가 커밋해 만료된 속성을 다시 읽는다
    assert meeting.status == "PAUSED" and meeting.paused_at == T0 + timedelta(seconds=60)

    # 40.7초 멈춘 뒤 재개: 정지 시간은 내림해 pausedTotalSeconds 에만 더한다
    monkeypatch.setattr(timer, "utcnow", lambda: T0 + timedelta(seconds=100.7))
    res = await client.post(f"/api/v1/meetings/{code}/resume", headers=bearer(token))
    assert res.status_code == 200
    resumed = res.json()["data"]
    assert resumed["timer"]["status"] == "IN_PROGRESS"
    assert resumed["timer"]["pausedTotalSeconds"] == 40
    assert resumed["timer"]["totalElapsedSeconds"] == 60
    assert resumed["timer"]["agendaElapsedSeconds"] == 60
    await session.refresh(meeting)
    assert meeting.status == "IN_PROGRESS" and meeting.paused_at is None


async def test_events_go_to_everyone(app: FastAPI) -> None:
    async with room(app) as r:
        _, member_token = await r.member("민지")
        async with r.connect(r.host_token) as host:
            await ready(host)
            async with r.connect(member_token) as member:
                await ready(member)
                await ready(host)  # participant.joined 를 비운다

                res = await r.client.post(f"/api/v1/meetings/{r.code}/pause", headers=bearer(r.host_token))
                for ws in (host, member):
                    event = await receive(ws)
                    assert event["type"] == "meeting.paused"
                    assert event["data"] == res.json()["data"]

                res = await r.client.post(f"/api/v1/meetings/{r.code}/resume", headers=bearer(r.host_token))
                for ws in (host, member):
                    event = await receive(ws)
                    assert event["type"] == "meeting.resumed"
                    assert event["data"] == res.json()["data"]


async def test_wrong_state_is_409(client: httpx.AsyncClient) -> None:
    code, token = await create(client)
    res = await client.post(f"/api/v1/meetings/{code}/resume", headers=bearer(token))
    assert (res.status_code, res.json()["error"]["code"]) == (409, "INVALID_STATE")

    await client.post(f"/api/v1/meetings/{code}/pause", headers=bearer(token))
    res = await client.post(f"/api/v1/meetings/{code}/pause", headers=bearer(token))
    assert (res.status_code, res.json()["error"]["code"]) == (409, "INVALID_STATE")


@pytest.mark.parametrize("action", ["pause", "resume"])
async def test_ended_meeting_is_410(client: httpx.AsyncClient, session: AsyncSession, action: str) -> None:
    code, token = await create(client)
    meeting = await session.get(Meeting, code)
    assert meeting is not None
    meeting.status, meeting.ended_at, meeting.ended_by = "ENDED", timer.utcnow(), "HOST"
    await session.flush()

    res = await client.post(f"/api/v1/meetings/{code}/{action}", headers=bearer(token))
    assert (res.status_code, res.json()["error"]["code"]) == (410, "MEETING_ENDED")


@pytest.mark.parametrize("action", ["pause", "resume"])
async def test_host_only(client: httpx.AsyncClient, action: str) -> None:
    code, _ = await create(client)
    member = (await client.post(f"/api/v1/meetings/{code}/sign-in", json={"name": "민지"})).json()["data"]

    res = await client.post(f"/api/v1/meetings/{code}/{action}", headers=bearer(member["accessToken"]))
    assert (res.status_code, res.json()["error"]["code"]) == (403, "HOST_ONLY")
    res = await client.post(f"/api/v1/meetings/{code}/{action}")
    assert (res.status_code, res.json()["error"]["code"]) == (401, "UNAUTHORIZED")
