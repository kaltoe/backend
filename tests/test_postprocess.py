"""CLOVA 후처리(#26). CLOVA 응답은 직접 만든 가짜 데이터만 쓴다."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import summary
from app.config import get_settings
from app.db.models import Agenda, Meeting, Utterance
from app.db.session import SessionFactory
from app.meetings import postprocess
from app.stt import clova
from tests.conftest import started
from tests.test_agendas import T0, THREE_AGENDAS, bearer

URL = "/api/v1/internal/clova/callback"


@pytest.fixture(autouse=True)
def secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "clova_callback_secret", "test-secret")
    monkeypatch.setattr(get_settings(), "recordings_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def sessions(session: AsyncSession) -> SessionFactory:
    @asynccontextmanager
    async def borrowed() -> AsyncIterator[AsyncSession]:
        yield session

    return borrowed


class Clova:
    """가짜 CLOVA 업로드. fail 만큼 먼저 실패한다."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[Path, int | None, dict[str, Any]]] = []
        self.fail = 0
        monkeypatch.setattr(clova, "request", self)

    async def __call__(self, path: Path, settings: Any, speakers: int | None, userdata: dict[str, Any]) -> None:
        self.calls.append((path, speakers, userdata))
        if self.fail:
            self.fail -= 1
            raise OSError("CLOVA 접속 실패")


@pytest.fixture
def fake_clova(monkeypatch: pytest.MonkeyPatch) -> Clova:
    return Clova(monkeypatch)


async def ended(client: httpx.AsyncClient, session: AsyncSession, body: dict[str, Any] = THREE_AGENDAS) -> str:
    """종료된 회의. 안건 녹음 구간은 0~10분, 10~22분이고 세 번째 안건은 끝내지 못했다."""
    created = (await started(client, body)).json()["data"]
    code: str = created["meetingId"]
    await client.post(f"/api/v1/meetings/{code}/end", headers=bearer(created["accessToken"]))
    agendas = list(await session.scalars(select(Agenda).where(Agenda.meeting_id == code).order_by(Agenda.order_index)))
    for agenda, span in zip(agendas, [(0, 600_000), (600_000, 1_320_000), (None, None)], strict=True):
        await session.refresh(agenda)
        agenda.recording_start_ms, agenda.recording_end_ms = span
        agenda.status = "PENDING" if span[0] is None else "DONE"
    await session.flush()
    return code


async def reload(session: AsyncSession, code: str) -> Meeting:
    meeting = await session.get(Meeting, code)
    assert meeting is not None
    await session.refresh(meeting)
    return meeting


def flac(recordings: Path, code: str) -> Path:
    path = recordings / code / "merged.flac"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC fake")
    return path


def callback(
    code: str, segments: list[dict[str, Any]], result: str = "COMPLETED", token: str | None = None
) -> dict[str, Any]:
    userdata = {"meetingId": code, "callbackToken": token or postprocess.callback_token(code)}
    return {"result": result, "message": "Succeeded", "userdata": userdata, "segments": segments, "text": "…"}


SEGMENTS = [
    {"start": 95_000, "end": 98_000, "text": "지난주 캠페인 CTR이 목표보다 높게 나왔어요.", "speaker": {"label": "1"}},
    {"start": 650_000, "end": 652_500, "text": "예산은 광고비부터 볼게요.", "speaker": {"label": "2"}},
    {"start": 1_400_000, "end": 1_401_000, "text": "끝내지 못한 안건 시간", "speaker": {"label": "1"}},
]


async def test_request_uploads_flac_with_attendee_count_and_token(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, secrets: Path, fake_clova: Clova
) -> None:
    code = await ended(client, session, THREE_AGENDAS | {"attendeeCount": 4})
    path = flac(secrets, code)
    fake_clova.fail = 2  # 두 번 실패해도 세 번째에 접수
    await postprocess.request(sessions, code)

    assert len(fake_clova.calls) == 3
    sent_path, speakers, userdata = fake_clova.calls[-1]
    assert (sent_path, speakers) == (path, 4)
    assert userdata == {"meetingId": code, "callbackToken": postprocess.callback_token(code)}
    assert not path.exists()  # 접수되면 지운다
    assert (await reload(session, code)).postprocess_status == "PROCESSING"  # 결과는 콜백으로


async def test_request_rules(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory, secrets: Path, fake_clova: Clova
) -> None:
    # 비대면은 보내지 않는다
    remote = await ended(client, session, THREE_AGENDAS | {"meetingMode": "REMOTE"})
    flac(secrets, remote)
    await postprocess.request(sessions, remote)
    assert fake_clova.calls == []
    assert (await reload(session, remote)).postprocess_status == "NONE"

    # attendeeCount 가 없으면 화자 수 자동(None)
    auto = await ended(client, session)
    flac(secrets, auto)
    await postprocess.request(sessions, auto)
    assert fake_clova.calls[0][1] is None

    # 3번 실패하면 FAILED, 다시 보낼 수 있게 flac 은 남긴다
    broken = await ended(client, session)
    path = flac(secrets, broken)
    fake_clova.fail = 3
    await postprocess.request(sessions, broken)
    assert (await reload(session, broken)).postprocess_status == "FAILED"
    assert path.exists()

    # 합본 flac 이 없으면 FAILED
    missing = await ended(client, session)
    await postprocess.request(sessions, missing)
    assert (await reload(session, missing)).postprocess_status == "FAILED"


async def test_callback_saves_segments_by_agenda_once(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    refreshed: list[str] = []
    monkeypatch.setattr(summary, "refresh_later", refreshed.append)
    code = await ended(client, session)
    res = await client.post(URL, json=callback(code, SEGMENTS))
    assert refreshed == [code]  # 저장한 뒤 요약을 화자 분리 결과로 다시 만든다(#27)
    assert (res.status_code, res.json()) == (200, {"received": True})  # 공통 응답 형식을 쓰지 않는다

    first, second, _ = [
        a.id
        for a in await session.scalars(select(Agenda).where(Agenda.meeting_id == code).order_by(Agenda.order_index))
    ]
    rows = list(
        await session.scalars(
            select(Utterance)
            .where(Utterance.meeting_id == code, Utterance.source == "POSTPROCESS")
            .order_by(Utterance.offset_ms)
        )
    )
    assert [(r.agenda_id, r.offset_ms, r.duration_ms, r.speaker_label, r.participant_id) for r in rows] == [
        (first, 95_000, 3000, "화자 1", None),
        (second, 650_000, 2500, "화자 2", None),
        (None, 1_400_000, 1000, "화자 1", None),  # 어느 안건 구간에도 없다
    ]
    assert rows[0].text == "지난주 캠페인 CTR이 목표보다 높게 나왔어요."
    assert (await reload(session, code)).postprocess_status == "DONE"

    # 같은 콜백이 다시 오면 저장하지 않고 200
    again = await client.post(URL, json=callback(code, SEGMENTS))
    assert (again.status_code, again.json()) == (200, {"received": True})
    assert refreshed == [code]  # 중복 콜백은 다시 만들지 않는다
    count = len(
        list(
            await session.scalars(
                select(Utterance.id).where(Utterance.source == "POSTPROCESS", Utterance.meeting_id == code)
            )
        )
    )
    assert count == 3


async def test_callback_rejects_wrong_token(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = await ended(client, session)
    assert (await client.post(URL, json=callback(code, SEGMENTS, token="forged"))).status_code == 403
    assert (await client.post(URL, json={"result": "COMPLETED"})).status_code == 403
    assert (await client.post(URL, content=b"not json")).status_code == 403
    assert (await reload(session, code)).postprocess_status == "PROCESSING"
    monkeypatch.setattr(get_settings(), "clova_callback_secret", "")  # 비밀키가 없으면 아무것도 받지 않는다
    assert (
        await client.post(URL, json=callback(code, SEGMENTS, token=postprocess.callback_token(code)))
    ).status_code == 403


async def test_callback_failed_result_and_not_processing(client: httpx.AsyncClient, session: AsyncSession) -> None:
    code = await ended(client, session)
    res = await client.post(URL, json=callback(code, [], result="FAILED"))
    assert res.json() == {"received": True}
    assert (await reload(session, code)).postprocess_status == "FAILED"

    remote = await ended(client, session, THREE_AGENDAS | {"meetingMode": "REMOTE"})  # postprocess NONE
    assert (await client.post(URL, json=callback(remote, SEGMENTS))).json() == {"received": True}
    assert list(await session.scalars(select(Utterance.id).where(Utterance.meeting_id == remote))) == []


async def test_expire_after_30_minutes(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory
) -> None:
    late, waiting, merging = (
        (await ended(client, session)),
        (await ended(client, session)),
        (await ended(client, session)),
    )
    for code, minutes, recording in ((late, 31, "READY"), (waiting, 10, "READY"), (merging, 31, "PROCESSING")):
        meeting = await reload(session, code)
        meeting.ended_at, meeting.recording_status = T0 - timedelta(minutes=minutes), recording
    await session.flush()
    assert await postprocess.expire(sessions, T0) == [late]
    assert [(await reload(session, c)).postprocess_status for c in (late, waiting, merging)] == [
        "FAILED",
        "PROCESSING",
        "PROCESSING",  # 합본이 아직 만드는 중이면 기다린다
    ]
