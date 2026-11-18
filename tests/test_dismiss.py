import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import offtopic
from app.api.schemas import to_kst_iso
from app.config import get_settings
from app.db.models import Agenda, Meeting, OfftopicEvent, Participant
from app.db.session import SessionFactory, get_engine, get_sessionmaker
from app.main import create_app
from app.realtime.manager import manager
from tests.conftest import started
from tests.test_agendas import T0, at, bearer, error
from tests.test_offtopic import BODY, NOW, Model, _forget_judges, later, model, say, sessions, setup

__all__ = ["_forget_judges", "model", "sessions"]  # test_offtopic 의 픽스처를 여기서도 쓴다


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    sent: list[tuple[str, Any]] = []

    async def capture(meeting_id: str, type_: str, data: Any = None) -> None:
        if type_.startswith("offtopic."):  # 회의 시작(meeting.started) 등은 보지 않는다
            sent.append((type_, data))

    monkeypatch.setattr(manager, "broadcast", capture)
    return sent


async def warn(session: AsyncSession, code: str, agenda_id: int) -> int:
    event = OfftopicEvent(meeting_id=code, agenda_id=agenda_id, score=0.8, detected_at=NOW)
    session.add(event)
    await session.flush()
    return event.id


async def dismiss(client: httpx.AsyncClient, code: str, event_id: int, token: str | None) -> httpx.Response:
    headers = bearer(token) if token else {}
    return await client.post(f"/api/v1/meetings/{code}/offtopic-events/{event_id}/dismiss", headers=headers)


async def test_first_dismiss_records_and_tells_everyone_once(
    client: httpx.AsyncClient, session: AsyncSession, events: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, agenda_id, host_token = await setup(client, session)
    member = (await client.post(f"/api/v1/meetings/{code}/sign-in", json={"name": "민지"})).json()["data"]
    event_id = await warn(session, code, agenda_id)

    at(monkeypatch, 106)
    res = await dismiss(client, code, event_id, member["accessToken"])
    assert res.status_code == 200
    assert res.json()["data"] == {"eventId": event_id, "dismissedAt": to_kst_iso(T0 + timedelta(seconds=106))}
    assert [(t, d.event_id, d.dismissed_by.participant_id, d.dismissed_by.name) for t, d in events] == [
        ("offtopic.dismissed", event_id, member["participantId"], "민지")
    ]

    # 이미 닫혔으면 같은 200, 닫은 사람·시각은 그대로, 이벤트는 다시 보내지 않는다
    at(monkeypatch, 110)
    again = await dismiss(client, code, event_id, host_token)
    assert again.json()["data"] == res.json()["data"]
    assert len(events) == 1
    row = await session.get(OfftopicEvent, event_id)
    assert row is not None
    await session.refresh(row)
    assert (row.dismissed_by, row.dismissed_at) == (member["participantId"], T0 + timedelta(seconds=106))


async def test_not_this_meetings_warning(client: httpx.AsyncClient, session: AsyncSession) -> None:
    code, _, token = await setup(client, session)
    other, other_agenda, _ = await setup(client, session)
    other_event = await warn(session, other, other_agenda)
    assert error(await dismiss(client, code, 999_999, token)) == (404, "OFFTOPIC_EVENT_NOT_FOUND")
    assert error(await dismiss(client, code, other_event, token)) == (404, "OFFTOPIC_EVENT_NOT_FOUND")
    assert error(await dismiss(client, code, other_event, None)) == (401, "UNAUTHORIZED")
    assert error(await client.post(f"/api/v1/meetings/{code}/offtopic-events/abc/dismiss", headers=bearer(token))) == (
        400,
        "VALIDATION_ERROR",
    )


async def test_dismiss_quiets_judging_for_the_agenda(
    client: httpx.AsyncClient,
    session: AsyncSession,
    sessions: SessionFactory,
    model: Model,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, agenda_id, token = await setup(client, session)
    await say(session, code, agenda_id, 150_000)
    event_id = await warn(session, code, agenda_id)
    at(monkeypatch, 100)  # NOW 에 닫는다
    await dismiss(client, code, event_id, token)

    await offtopic.check(sessions, code, later(59))  # 60초 동안 같은 안건은 판정하지 않는다
    assert model.inputs == []
    await offtopic.check(sessions, code, later(61))
    assert len(model.inputs) == 1

    # 서버 설정으로 바꿀 수 있다
    monkeypatch.setattr(get_settings(), "offtopic_dismiss_quiet_seconds", 100)
    offtopic.judges.clear()
    await offtopic.check(sessions, code, later(61))
    assert len(model.inputs) == 1


async def test_many_people_at_once_dismiss_once(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """여러 명이 동시에 눌러도 한 번만 닫히고 이벤트도 한 번. 실제 연결 풀로 커밋하고 끝나면 지운다."""
    sent: list[str] = []

    async def capture(meeting_id: str, type_: str, data: Any = None) -> None:
        if type_.startswith("offtopic."):
            sent.append(type_)

    monkeypatch.setattr(manager, "broadcast", capture)
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    code = None
    try:
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = (await started(client, BODY)).json()["data"]
            code = created["meetingId"]
            tokens = [created["accessToken"]]
            for name in ("민지", "준호", "서연", "도윤"):
                res = await client.post(f"/api/v1/meetings/{code}/sign-in", json={"name": name})
                tokens.append(res.json()["data"]["accessToken"])
            async with get_sessionmaker()() as s:
                event = OfftopicEvent(
                    meeting_id=code, agenda_id=created["agendas"][0]["agendaId"], score=0.8, detected_at=NOW
                )
                s.add(event)
                await s.commit()
                event_id = event.id

            results = await asyncio.gather(*(dismiss(client, code, event_id, t) for t in tokens))
            assert [r.status_code for r in results] == [200] * 5
            assert len({r.json()["data"]["dismissedAt"] for r in results}) == 1
            assert sent == ["offtopic.dismissed"]
    finally:
        if code is not None:
            async with get_sessionmaker()() as s:
                for model in (OfftopicEvent, Agenda, Participant, Meeting):
                    column = model.id if model is Meeting else model.meeting_id
                    await s.execute(delete(model).where(column == code))
                await s.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
