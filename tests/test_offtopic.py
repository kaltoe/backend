from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import offtopic
from app.db.models import Meeting, MeetingStatus, OfftopicEvent, Utterance
from app.db.session import SessionFactory
from app.meetings import timer
from app.realtime.manager import manager
from tests.test_agendas import T0, at, bearer, start_at_t0

BODY = {
    "title": "마케팅 전략 주간 회의",
    "consentAgreed": True,
    "agendas": [
        {"title": "캠페인 리뷰", "keywords": ["CTR", "전환율"], "allocatedMinutes": 10},
        {"title": "예산", "keywords": [], "allocatedMinutes": 12},
    ],
}
NOW = T0 + timedelta(seconds=100)  # 녹음 위치 100초
TALK = "지난주 캠페인 클릭률이 목표보다 높았어요"  # 15자 넘는 대화
LOW = {"q_current": 0.1, "q_chat": 0.1}
HIGH = {"q_current": 0.9, "q_chat": 0.9}


@pytest.fixture(autouse=True)
def _forget_judges(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(offtopic, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    offtopic.judges.clear()
    yield
    offtopic.judges.clear()


@pytest.fixture
def sessions(session: AsyncSession) -> SessionFactory:
    @asynccontextmanager
    async def borrowed() -> AsyncIterator[AsyncSession]:
        yield session

    return borrowed


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    sent: list[tuple[str, Any]] = []

    async def capture(meeting_id: str, type_: str, data: Any = None) -> None:
        sent.append((type_, data))

    monkeypatch.setattr(manager, "broadcast", capture)
    return sent


class Model:
    """가짜 Decisions. answers 를 차례로 돌려주고(없으면 LOW), 예외를 넣으면 그 호출은 실패한다."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.answers: list[dict[str, float] | Exception] = []
        self.inputs: list[str] = []
        monkeypatch.setattr(offtopic, "ask_model", self)

    async def __call__(self, text: str) -> dict[str, float]:
        self.inputs.append(text)
        answer = self.answers.pop(0) if self.answers else LOW
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> Model:
    return Model(monkeypatch)


async def setup(client: httpx.AsyncClient, session: AsyncSession, body: dict[str, Any] = BODY) -> tuple[str, int, str]:
    created = (await client.post("/api/v1/meetings", json=body)).json()["data"]
    await start_at_t0(session, created["meetingId"])
    return created["meetingId"], created["agendas"][0]["agendaId"], created["accessToken"]


async def say(session: AsyncSession, code: str, agenda_id: int, offset_ms: int, text: str = TALK, **kw: Any) -> None:
    session.add(
        Utterance(
            meeting_id=code,
            agenda_id=agenda_id,
            source="REALTIME",
            text=text,
            offset_ms=offset_ms,
            duration_ms=3000,
            **kw,
        )
    )
    await session.flush()


def of(events: list[tuple[str, Any]], type_: str) -> list[Any]:
    return [data for t, data in events if t == type_]


def later(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


async def test_either_question_counts_and_two_in_a_row_warns(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, model: Model, events: list[Any]
) -> None:
    code, agenda_id, _ = await setup(client, session)
    await say(session, code, agenda_id, 90_000)
    model.answers = [
        {"q_current": 0.7, "q_chat": 0.1},  # 높음(현재 안건과 무관)
        LOW,  # 끊김
        {"q_current": 0.2, "q_chat": 0.6},  # 높음(잡담)
        {"q_current": 0.8, "q_chat": 0.3},  # 높음 → 2회 연속
    ]
    for _ in range(4):
        await offtopic.check(sessions, code, NOW)

    warnings = of(events, "offtopic.warning")
    assert len(warnings) == 1
    warning = warnings[0]
    assert (warning.agenda_id, warning.agenda_title, warning.score, warning.auto_close_seconds) == (
        agenda_id,
        "캠페인 리뷰",
        0.8,  # 두 질문 중 큰 값
        5,
    )
    rows = list(await session.scalars(select(OfftopicEvent).where(OfftopicEvent.meeting_id == code)))
    assert [(r.id, r.score, r.window_text) for r in rows] == [(warning.event_id, pytest.approx(0.8), TALK)]

    # 입력에 회의 제목, 전체 안건(현재 표시), 현재 안건 키워드가 들어간다
    assert model.inputs[0] == (
        "회의 제목: 마케팅 전략 주간 회의\n"
        "전체 안건: 1. 캠페인 리뷰 (현재)  2. 예산\n"
        "현재 안건: 캠페인 리뷰\n"
        "현재 안건 키워드: CTR, 전환율\n"
        f"최근 대화:\n{TALK}"
    )


async def test_no_warning_when_meeting_changed_while_model_was_answering(
    client: httpx.AsyncClient,
    session: AsyncSession,
    sessions: SessionFactory,
    model: Model,
    events: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """모델 호출이 길어지는 사이 일시 정지됐으면 이전 판정으로 경고하지 않고, 쉬는 시간도 걸지 않는다."""
    code, agenda_id, _ = await setup(client, session)
    await say(session, code, agenda_id, 90_000)
    model.answers = [HIGH]
    await offtopic.check(sessions, code, NOW)  # 높음 1

    async def pause_then_answer(text: str) -> dict[str, float]:  # 답을 기다리는 사이 진행자가 일시 정지
        meeting = await session.get(Meeting, code)
        assert meeting is not None
        meeting.status, meeting.paused_at = MeetingStatus.PAUSED.value, NOW
        await session.flush()
        return HIGH

    monkeypatch.setattr(offtopic, "ask_model", pause_then_answer)
    await offtopic.check(sessions, code, NOW)

    assert of(events, "offtopic.warning") == []
    assert list(await session.scalars(select(OfftopicEvent).where(OfftopicEvent.meeting_id == code))) == []
    assert offtopic.judges[code].quiet_until is None


async def test_after_warning_rests_30_seconds_and_counts_from_zero(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, model: Model, events: list[Any]
) -> None:
    code, agenda_id, _ = await setup(client, session)
    await say(session, code, agenda_id, 90_000)
    await say(session, code, agenda_id, 125_000)
    model.answers = [HIGH] * 5
    await offtopic.check(sessions, code, NOW)
    await offtopic.check(sessions, code, NOW)  # 경고
    calls = len(model.inputs)
    await offtopic.check(sessions, code, later(29))  # 30초 동안은 부르지 않는다
    assert len(model.inputs) == calls
    await offtopic.check(sessions, code, later(31))  # 다시 판정하지만 0부터 센다
    assert len(model.inputs) == calls + 1
    assert len(of(events, "offtopic.warning")) == 1
    await offtopic.check(sessions, code, later(31))
    assert len(of(events, "offtopic.warning")) == 2


async def test_skips_grace_quiet_dismissed_and_paused(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, model: Model, events: list[Any]
) -> None:
    code, agenda_id, token = await setup(client, session)
    await say(session, code, agenda_id, 10_000)
    await say(session, code, agenda_id, 90_000, "짧은 말")  # 15자 미만

    # 안건이 시작된 지 20초 안
    await offtopic.check(sessions, code, T0 + timedelta(seconds=15))
    # 최근 30초 대화가 15자 미만
    await offtopic.check(sessions, code, NOW)
    assert model.inputs == []

    # 같은 안건에서 [관련 있는 얘기예요]를 누른 뒤 60초
    await say(session, code, agenda_id, 150_000)
    session.add(OfftopicEvent(meeting_id=code, agenda_id=agenda_id, score=0.9, detected_at=NOW, dismissed_at=NOW))
    await session.flush()
    await offtopic.check(sessions, code, later(55))
    assert model.inputs == []
    await offtopic.check(sessions, code, later(61))
    assert len(model.inputs) == 1

    # 일시 정지 중
    await client.post(f"/api/v1/meetings/{code}/pause", headers=bearer(token))
    await offtopic.check(sessions, code, later(61))
    assert len(model.inputs) == 1


async def test_window_stays_in_current_agenda_and_streak_resets(
    client: httpx.AsyncClient,
    session: AsyncSession,
    sessions: SessionFactory,
    model: Model,
    events: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, first, token = await setup(client, session)
    await say(session, code, first, 95_000, "이전 안건에서 한 이야기입니다 길게요")
    model.answers = [HIGH]
    await offtopic.check(sessions, code, NOW)  # 첫 안건에서 높음 1

    at(monkeypatch, 100)  # 녹음 위치 100초에 다음 안건
    res = await client.post(
        f"/api/v1/meetings/{code}/agendas/next", json={"fromAgendaId": first}, headers=bearer(token)
    )
    second = res.json()["data"]["currentAgenda"]["agendaId"]
    await say(session, code, second, 115_000, "예산은 광고비부터 정해야 할 것 같아요")
    await offtopic.check(sessions, code, later(10))  # 새 안건 20초 안 → 판정 안 함
    assert len(model.inputs) == 1

    model.answers = [HIGH]
    await offtopic.check(sessions, code, later(25))
    # 창은 새 안건 시작(100초)부터: 이전 안건 이야기는 빠지고, 안건이 바뀌어 연속도 0부터
    assert model.inputs[1].endswith("최근 대화:\n예산은 광고비부터 정해야 할 것 같아요")
    assert "현재 안건: 예산" in model.inputs[1]
    assert of(events, "offtopic.warning") == []


async def test_error_after_three_failed_checks_and_ok_after_success(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, model: Model, events: list[Any]
) -> None:
    code, agenda_id, _ = await setup(client, session)
    await say(session, code, agenda_id, 90_000)
    # 판정 한 번은 최초 1회 + 재시도 3회 = 4번 부른다
    model.answers = [RuntimeError("timeout")] * 4 * 3
    for _ in range(3):
        await offtopic.check(sessions, code, NOW)
    assert len(model.inputs) == 12
    assert [e.state for e in of(events, "offtopic.status")] == ["ERROR"]

    model.answers = [RuntimeError("timeout"), LOW]  # 재시도로 성공
    await offtopic.check(sessions, code, NOW)
    assert [e.state for e in of(events, "offtopic.status")] == ["ERROR", "OK"]
    assert offtopic.state(code) == "OK"


async def test_remote_window_merges_devices_in_time_order(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, model: Model
) -> None:
    code, agenda_id, _ = await setup(client, session, BODY | {"meetingMode": "REMOTE"})
    await say(session, code, agenda_id, 95_000, "저는 반대 의견이에요", speaker_label="민지")
    await say(session, code, agenda_id, 80_000, "예산을 먼저 정해야 할 것 같아요", speaker_label="진행자")
    await say(session, code, agenda_id, 60_000, "30초보다 전에 끝난 말", speaker_label="민지")
    await say(session, code, agenda_id, 68_000, "창에 1초 넘게 걸친 말", speaker_label="민지")  # 68~71초
    await offtopic.check(sessions, code, NOW)
    assert model.inputs[0].endswith(
        "최근 대화:\n"
        "[민지] 창에 1초 넘게 걸친 말\n[진행자] 예산을 먼저 정해야 할 것 같아요\n[민지] 저는 반대 의견이에요"
    )


async def test_busy_meeting_is_not_judged_twice(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _, _ = await setup(client, session)
    spawned: list[Coroutine[Any, Any, None]] = []
    monkeypatch.setattr(offtopic, "spawn", spawned.append)
    offtopic.judges[code] = offtopic.Judge(busy=True)  # 재시도 중
    await offtopic.sweep(sessions, NOW)
    assert spawned == []
    offtopic.judges[code].busy = False
    await offtopic.sweep(sessions, NOW)
    assert len(spawned) == 1 and offtopic.judges[code].busy
    spawned[0].close()


async def test_state_shows_status_and_active_warning(client: httpx.AsyncClient, session: AsyncSession) -> None:
    code, agenda_id, token = await setup(client, session)

    async def state() -> dict[str, Any]:
        res = await client.get(f"/api/v1/meetings/{code}/state", headers=bearer(token))
        data: dict[str, Any] = res.json()["data"]
        return data

    now = timer.utcnow()
    old = OfftopicEvent(meeting_id=code, agenda_id=agenda_id, score=0.7, detected_at=now - timedelta(seconds=6))
    fresh = OfftopicEvent(meeting_id=code, agenda_id=agenda_id, score=0.8, detected_at=now - timedelta(seconds=1))
    session.add_all([old, fresh])
    await session.flush()
    offtopic.judges[code] = offtopic.Judge(state="ERROR")

    data = await state()
    assert data["offtopicStatus"] == "ERROR"
    active = data["activeOfftopicEvent"]
    assert (active["eventId"], active["agendaTitle"], active["score"], active["autoCloseSeconds"]) == (
        fresh.id,
        "캠페인 리뷰",
        pytest.approx(0.8),
        5,
    )

    # 닫으면(또는 5초가 지나면) null
    fresh.dismissed_at = now
    await session.flush()
    assert (await state())["activeOfftopicEvent"] is None
