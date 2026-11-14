from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Agenda, Meeting, Utterance
from tests.test_agendas import bearer, create, error


async def setup(session: AsyncSession, client: httpx.AsyncClient, mode: str = "IN_PERSON") -> dict[str, Any]:
    created = await create(client)
    code = created["meetingId"]
    meeting = await session.get(Meeting, code)
    assert meeting is not None
    meeting.meeting_mode = mode
    a0, a1, a2 = (await session.scalars(select(Agenda).where(Agenda.meeting_id == code).order_by("order_index"))).all()
    rows = [
        # (agenda, source, speaker, text, offset) — 넣는 순서와 offset 순서를 일부러 다르게
        (a1, "REALTIME", None, "예산 얘기", 700_000),
        (a0, "REALTIME", None, "리뷰 시작", 1_000),
        (a0, "REALTIME", None, "리뷰 둘째", 5_000),
        (a1, "POSTPROCESS", "화자 2", "예산은", 710_000),
        (None, "POSTPROCESS", "화자 1", "구간 밖(앞)", 0),
        (a0, "POSTPROCESS", "화자 1", "리뷰는", 2_000),
        (None, "POSTPROCESS", "화자 2", "구간 밖(사이)", 705_000),
    ]
    session.add_all(
        Utterance(meeting_id=code, agenda_id=a.id if a else None, source=s, speaker_label=sp, text=t, offset_ms=o)
        for a, s, sp, t, o in rows
    )
    await session.flush()
    created["agendaIds"] = [a0.id, a1.id, a2.id]
    return created


async def transcripts(client: httpx.AsyncClient, created: dict[str, Any], query: str = "") -> httpx.Response:
    return await client.get(
        f"/api/v1/meetings/{created['meetingId']}/transcripts{query}", headers=bearer(created["accessToken"])
    )


def texts(data: dict[str, Any]) -> list[tuple[int, list[str]]]:
    return [(g["agendaId"], [i["text"] for i in g["items"]]) for g in data["groups"]]


async def test_auto_picks_realtime_until_postprocess_done(client: httpx.AsyncClient, session: AsyncSession) -> None:
    created = await setup(session, client)
    a0, a1, _ = created["agendaIds"]

    data = (await transcripts(client, created)).json()["data"]
    assert data["source"] == "REALTIME"
    # 안건 순서·offset 순서, 발화가 없는 안건(a2)은 빠짐, 대면 실시간은 화자 없음
    assert texts(data) == [(a0, ["리뷰 시작", "리뷰 둘째"]), (a1, ["예산 얘기"])]
    group = data["groups"][0]
    assert (group["orderIndex"], group["title"]) == (0, "리뷰")
    item = group["items"][0]
    assert set(item) == {"utteranceId", "speakerLabel", "text", "offsetMs"}
    assert (item["speakerLabel"], item["offsetMs"]) == (None, 1_000)

    meeting = await session.get(Meeting, created["meetingId"])
    assert meeting is not None
    meeting.postprocess_status = "DONE"
    await session.flush()
    data = (await transcripts(client, created)).json()["data"]
    assert data["source"] == "POSTPROCESS"
    # agenda_id NULL 은 바로 앞 발화의 안건, 맨 앞이면 첫 안건
    assert texts(data) == [(a0, ["구간 밖(앞)", "리뷰는", "구간 밖(사이)"]), (a1, ["예산은"])]
    assert data["groups"][0]["items"][0]["speakerLabel"] == "화자 1"


@pytest.mark.parametrize(
    ("query", "source"), [("?source=REALTIME", "REALTIME"), ("?source=POSTPROCESS", "POSTPROCESS")]
)
async def test_explicit_source(client: httpx.AsyncClient, session: AsyncSession, query: str, source: str) -> None:
    created = await setup(session, client)  # 후처리 전이어도 요청한 종류를 준다
    assert (await transcripts(client, created, query)).json()["data"]["source"] == source


async def test_remote_realtime_has_participant_name(client: httpx.AsyncClient, session: AsyncSession) -> None:
    created = await create(client)
    code = created["meetingId"]
    agenda = (await session.scalars(select(Agenda).where(Agenda.meeting_id == code))).first()
    assert agenda is not None
    session.add(
        Utterance(meeting_id=code, agenda_id=agenda.id, source="REALTIME", speaker_label="민지", text="네", offset_ms=0)
    )
    await session.flush()
    item = (await transcripts(client, created)).json()["data"]["groups"][0]["items"][0]
    assert item["speakerLabel"] == "민지"


async def test_empty_validation_and_auth(client: httpx.AsyncClient) -> None:
    created = await create(client)
    assert (await transcripts(client, created)).json()["data"] == {"source": "REALTIME", "groups": []}
    assert error(await transcripts(client, created, "?source=auto")) == (400, "VALIDATION_ERROR")
    assert error(await transcripts(client, created, "?source=CLOVA")) == (400, "VALIDATION_ERROR")
    res = await client.get(f"/api/v1/meetings/{created['meetingId']}/transcripts")
    assert error(res) == (401, "UNAUTHORIZED")
