import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import summary
from app.db.models import Agenda, Meeting, Utterance
from app.db.session import SessionFactory
from tests.test_agendas import THREE_AGENDAS, bearer, error, go_next


@pytest.fixture
def sessions(session: AsyncSession) -> SessionFactory:
    @asynccontextmanager
    async def borrowed() -> AsyncIterator[AsyncSession]:
        yield session

    return borrowed


class Model:
    """가짜 요약 모델. 안건 제목으로 3줄을 만들고, fail 이면 예외."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str, list[str], str]] = []
        self.fail = False
        self.answer: list[str] | None = None  # 정하면 안건과 상관없이 이 3줄
        monkeypatch.setattr(summary, "ask_model", self)

    async def __call__(
        self, meeting_title: str, agenda_title: str, keywords: list[str], conversation: str
    ) -> list[str]:
        self.calls.append((meeting_title, agenda_title, keywords, conversation))
        if self.fail:
            raise RuntimeError("OpenAI 500")
        return self.answer or [f"{agenda_title} 1줄", f"{agenda_title} 2줄", f"{agenda_title} 3줄"]


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> Model:
    return Model(monkeypatch)


async def ended(client: httpx.AsyncClient, body: dict[str, Any] = THREE_AGENDAS) -> tuple[dict[str, Any], list[int]]:
    """리뷰·예산 두 안건을 끝내고 종료. 세 번째(액션)는 끝내지 못했다."""
    created = (await client.post("/api/v1/meetings", json=body)).json()["data"]
    ids = [a["agendaId"] for a in created["agendas"]]
    await go_next(client, created, ids[0])
    await client.post(f"/api/v1/meetings/{created['meetingId']}/end", headers=bearer(created["accessToken"]))
    return created, ids


async def say(
    session: AsyncSession, code: str, agenda_id: int, text: str, source: str, speaker: str | None = None
) -> None:
    session.add(
        Utterance(meeting_id=code, agenda_id=agenda_id, source=source, text=text, offset_ms=1000, speaker_label=speaker)
    )
    await session.flush()


async def meeting(session: AsyncSession, code: str) -> Meeting:
    m = await session.get(Meeting, code)
    assert m is not None
    await session.refresh(m)
    return m


async def lines(session: AsyncSession, code: str) -> list[list[str] | None]:
    agendas = list(await session.scalars(select(Agenda).where(Agenda.meeting_id == code).order_by(Agenda.order_index)))
    for a in agendas:
        await session.refresh(a)
    return [a.summary_lines for a in agendas]


async def test_in_person_uses_clova_result_when_done(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, model: Model
) -> None:
    created, (review, budget, action) = await ended(client)
    code = created["meetingId"]
    await say(session, code, review, "실시간 자막 문장", "REALTIME")
    await say(session, code, review, "CLOVA 문장", "POSTPROCESS", "화자 1")
    await say(session, code, action, "끝내지 못한 안건 발언", "POSTPROCESS", "화자 2")
    m = await meeting(session, code)
    m.postprocess_status = "DONE"
    await session.flush()

    await summary.summarize(sessions, code)
    # 화자 분리 결과로, 완료된 안건만, 발언이 없던 예산 안건은 모델을 부르지 않고 []
    assert model.calls == [("마케팅 전략 주간 회의", "리뷰", [], "[화자 1] CLOVA 문장")]
    assert await lines(session, code) == [["리뷰 1줄", "리뷰 2줄", "리뷰 3줄"], [], None]
    assert (await meeting(session, code)).summary_status == "DONE"


@pytest.mark.parametrize(("mode", "postprocess"), [("IN_PERSON", "FAILED"), ("REMOTE", "NONE")])
async def test_realtime_when_clova_failed_or_remote(
    client: httpx.AsyncClient,
    session: AsyncSession,
    sessions: SessionFactory,
    model: Model,
    mode: str,
    postprocess: str,
) -> None:
    created, (review, _, _) = await ended(client, THREE_AGENDAS | {"meetingMode": mode})
    code = created["meetingId"]
    await say(session, code, review, "실시간 자막 문장", "REALTIME", "민지" if mode == "REMOTE" else None)
    await say(session, code, review, "CLOVA 문장", "POSTPROCESS", "화자 1")
    m = await meeting(session, code)
    m.postprocess_status = postprocess
    await session.flush()

    await summary.summarize(sessions, code)
    expected = "[민지] 실시간 자막 문장" if mode == "REMOTE" else "실시간 자막 문장"
    assert [c[3] for c in model.calls] == [expected]


async def test_failure_marks_failed_and_retry_reopens(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, model: Model
) -> None:
    created, (review, _, _) = await ended(client, THREE_AGENDAS | {"meetingMode": "REMOTE"})
    code, headers = created["meetingId"], bearer(created["accessToken"])
    await say(session, code, review, "실시간 자막 문장", "REALTIME")

    model.fail = True
    await summary.summarize(sessions, code)
    assert (await meeting(session, code)).summary_status == "FAILED"
    res = await client.get(f"/api/v1/meetings/{code}/summary", headers=headers)
    assert (res.status_code, res.json()["data"]) == (200, {"status": "FAILED", "agendas": []})  # 502 가 아니다

    # API-12: FAILED 에서만 202. 다시 PROCESSING 이 되고 요약 루프가 다시 만든다
    res = await client.post(f"/api/v1/meetings/{code}/summary/retry", headers=headers)
    assert (res.status_code, res.json()["data"]) == (202, {"status": "PROCESSING"})
    assert error(await client.post(f"/api/v1/meetings/{code}/summary/retry", headers=headers)) == (409, "INVALID_STATE")
    assert code in await summary.due(sessions)

    model.fail = False
    await summary.summarize(sessions, code)
    res = await client.get(f"/api/v1/meetings/{code}/summary", headers=headers)
    assert res.json()["data"] == {
        "status": "DONE",
        "agendas": [
            {"agendaId": review, "lines": ["리뷰 1줄", "리뷰 2줄", "리뷰 3줄"]},
            {"agendaId": created["agendas"][1]["agendaId"], "lines": []},
        ],
    }
    assert error(await client.post(f"/api/v1/meetings/{code}/summary/retry", headers=headers)) == (409, "INVALID_STATE")


async def test_summary_while_processing_and_auth(client: httpx.AsyncClient) -> None:
    created = (await client.post("/api/v1/meetings", json=THREE_AGENDAS)).json()["data"]
    code = created["meetingId"]
    res = await client.get(f"/api/v1/meetings/{code}/summary", headers=bearer(created["accessToken"]))
    assert res.json()["data"] == {"status": "PROCESSING", "agendas": []}  # 아직 끝나지 않은 회의
    assert error(await client.get(f"/api/v1/meetings/{code}/summary")) == (401, "UNAUTHORIZED")
    assert error(await client.post(f"/api/v1/meetings/{code}/summary/retry")) == (401, "UNAUTHORIZED")


async def test_due_right_after_end_for_both_modes(client: httpx.AsyncClient, sessions: SessionFactory) -> None:
    in_person, _ = await ended(client)  # 후처리 PROCESSING 이어도
    remote, _ = await ended(client, THREE_AGENDAS | {"meetingMode": "REMOTE"})
    live = (await client.post("/api/v1/meetings", json=THREE_AGENDAS)).json()["data"]  # 진행 중
    due = await summary.due(sessions)
    assert in_person["meetingId"] in due and remote["meetingId"] in due  # 둘 다 종료 직후 바로 만든다
    assert live["meetingId"] not in due


async def test_in_person_first_realtime_then_overwritten_by_clova(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, model: Model
) -> None:
    created, (review, _, _) = await ended(client)
    code = created["meetingId"]
    await say(session, code, review, "실시간 자막 문장", "REALTIME")

    # 종료 직후: CLOVA 를 기다리지 않고 실시간 발화로 먼저 만든다
    await summary.summarize(sessions, code)
    assert [c[3] for c in model.calls] == ["실시간 자막 문장"]
    assert (await meeting(session, code)).summary_status == "DONE"

    # CLOVA 결과가 저장되면 화자 분리 발화로 다시 만들어 덮어쓴다
    await say(session, code, review, "CLOVA 문장", "POSTPROCESS", "화자 1")
    m = await meeting(session, code)
    m.postprocess_status = "DONE"
    await session.flush()
    model.fail = True  # 다시 만들기가 실패하면 먼저 만든 요약을 그대로 둔다
    await summary.summarize(sessions, code, refresh=True)
    assert (await lines(session, code))[0] == ["리뷰 1줄", "리뷰 2줄", "리뷰 3줄"]
    assert (await meeting(session, code)).summary_status == "DONE"

    model.fail, model.answer = False, ["화자 분리 1줄", "화자 분리 2줄", "화자 분리 3줄"]
    model.calls.clear()
    await summary.summarize(sessions, code, refresh=True)
    assert [c[3] for c in model.calls] == ["[화자 1] CLOVA 문장"]
    assert (await lines(session, code))[0] == ["화자 분리 1줄", "화자 분리 2줄", "화자 분리 3줄"]


async def test_refresh_waits_for_first_summary_and_skips_without_clova(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, model: Model
) -> None:
    created, (review, _, _) = await ended(client)
    code = created["meetingId"]
    await say(session, code, review, "CLOVA 문장", "POSTPROCESS", "화자 1")

    # CLOVA 가 아직이면 다시 만들지 않는다
    m = await meeting(session, code)
    m.summary_status = "DONE"
    await session.flush()
    await summary.summarize(sessions, code, refresh=True)
    assert model.calls == []

    # 첫 요약이 돌고 있으면 끝날 때까지 기다린다
    m.postprocess_status = "DONE"
    await session.flush()
    summary.running.add(code)
    waiting = asyncio.create_task(summary.refresh_after(sessions, code))
    await asyncio.sleep(1.2)
    assert model.calls == []
    summary.running.discard(code)
    await waiting
    assert [c[3] for c in model.calls] == ["[화자 1] CLOVA 문장"]
