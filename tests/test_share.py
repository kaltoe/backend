import asyncio
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import to_kst_iso
from app.db.models import Agenda, Meeting, Participant, ShareLink
from app.db.session import get_engine, get_sessionmaker
from app.main import create_app
from app.meetings import share as share_module
from tests.test_agendas import T0, at, bearer, create, error, start_at_t0


async def end(client: httpx.AsyncClient, created: dict[str, Any]) -> None:
    res = await client.post(f"/api/v1/meetings/{created['meetingId']}/end", headers=bearer(created["accessToken"]))
    assert res.status_code == 200


async def share(client: httpx.AsyncClient, created: dict[str, Any]) -> httpx.Response:
    return await client.post(f"/api/v1/meetings/{created['meetingId']}/share", headers=bearer(created["accessToken"]))


async def test_link_expires_seven_days_after_end(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: Any
) -> None:
    created = await create(client)
    await start_at_t0(session, created["meetingId"])
    at(monkeypatch, 1600)
    await end(client, created)
    ended_at = T0 + timedelta(seconds=1600)

    at(monkeypatch, 1600 + 5 * 24 * 3600)  # 종료 5일 뒤에 만들어도 만료는 종료 + 7일
    first = (await share(client, created)).json()["data"]
    assert len(first["shareId"]) == 22
    assert first["shareUrl"] == f"http://localhost:3000/s/{first['shareId']}"
    assert first["expiresAt"] == to_kst_iso(ended_at + timedelta(days=7))
    link = await session.scalar(select(ShareLink).where(ShareLink.share_id == first["shareId"]))
    assert link is not None and link.created_by == created["host"]["participantId"]

    at(monkeypatch, 1600 + 7 * 24 * 3600 - 1)  # 만료 1초 전에는 같은 링크
    assert (await share(client, created)).json()["data"] == first

    at(monkeypatch, 1600 + 7 * 24 * 3600)  # 7일이 지나면 새 링크를 만들지 않는다
    response = await share(client, created)
    assert error(response) == (410, "SHARE_EXPIRED")
    assert response.json()["error"]["message"] == "공유 기간(회의 종료 후 7일)이 지났어요."
    assert list(await session.scalars(select(ShareLink.share_id))) == [first["shareId"]]


async def test_live_meeting_is_409_and_auth(client: httpx.AsyncClient) -> None:
    created = await create(client)
    assert error(await share(client, created)) == (409, "INVALID_STATE")
    await end(client, created)
    assert error(await client.post(f"/api/v1/meetings/{created['meetingId']}/share")) == (401, "UNAUTHORIZED")


async def test_concurrent_requests_make_one_link(db_url: str, monkeypatch: Any) -> None:
    """동시에 눌러도 회의 행 잠금 때문에 링크는 하나다. 실제 연결 풀로 커밋하고 끝나면 지운다(test_agendas 와 같음)."""
    real_valid_link = share_module.valid_link

    async def slow_valid_link(*args: Any) -> ShareLink | None:
        link = await real_valid_link(*args)
        await asyncio.sleep(0.05)  # 조회와 저장 사이에 다른 요청이 끼어들게 한다
        return link

    monkeypatch.setattr(share_module, "valid_link", slow_valid_link)
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    code = None
    try:
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await create(client)
            code = created["meetingId"]
            await end(client, created)
            results = await asyncio.gather(*(share(client, created) for _ in range(5)))
            assert {r.status_code for r in results} == {200}
            assert len({r.json()["data"]["shareId"] for r in results}) == 1
            async with get_sessionmaker()() as s:
                count = await s.scalar(select(func.count()).select_from(ShareLink).where(ShareLink.meeting_id == code))
            assert count == 1
    finally:
        if code is not None:
            async with get_sessionmaker()() as s:
                for model in (ShareLink, Agenda, Participant, Meeting):
                    column = model.id if model is Meeting else model.meeting_id
                    await s.execute(delete(model).where(column == code))
                await s.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
