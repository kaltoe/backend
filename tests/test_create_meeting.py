import asyncio
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import bcrypt
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import read_token
from app.api.errors import VALIDATION_MESSAGE
from app.db.models import Agenda, Meeting, Participant
from app.meetings import codes
from app.meetings.idempotency import IdempotencyCache, create_meeting_cache
from app.meetings.schemas import (
    AGENDA_LIMIT_MESSAGE,
    AGENDA_TITLE_MESSAGE,
    ALLOCATED_MESSAGE,
    KEYWORD_LIMIT_MESSAGE,
    TITLE_MESSAGE,
)

CODE = re.compile(r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$")
RECOVERY_KEY = re.compile(r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}$")


def payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "마케팅 전략 주간 회의",
        "targetTotalMinutes": 30,
        "meetingMode": "IN_PERSON",
        "attendeeCount": 4,
        "consentAgreed": True,
        "agendas": [
            {"title": "지난주 캠페인 성과 리뷰", "keywords": ["CTR", "전환율"], "allocatedMinutes": 10},
            {"title": "4분기 예산 검토", "keywords": ["예산", "광고비"], "allocatedMinutes": 12},
            {"title": "다음 주 액션 아이템", "keywords": ["담당자", "일정"], "allocatedMinutes": 8},
        ],
    }
    body.update(overrides)
    return body


def agenda(**overrides: Any) -> dict[str, Any]:
    return {"title": "안건", "keywords": [], "allocatedMinutes": 10, **overrides}


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    create_meeting_cache.clear()
    yield
    create_meeting_cache.clear()


async def meeting_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Meeting)) or 0


async def test_create_meeting_starts_meeting(client: httpx.AsyncClient, session: AsyncSession) -> None:
    res = await client.post("/api/v1/meetings", json=payload())
    assert res.status_code == 201
    body = res.json()
    assert body["success"] is True and body["error"] is None
    data = body["data"]
    assert set(data) == {
        "meetingId",
        "title",
        "meetingMode",
        "status",
        "invite",
        "timer",
        "agendas",
        "host",
        "accessToken",
        "hostRecoveryKey",
    }

    code = data["meetingId"]
    assert CODE.match(code)
    assert (data["title"], data["meetingMode"], data["status"]) == ("마케팅 전략 주간 회의", "IN_PERSON", "IN_PROGRESS")
    assert data["invite"]["code"] == code
    assert data["invite"]["url"] == f"http://localhost:3000/m/{code}"

    timer = data["timer"]
    first_id = data["agendas"][0]["agendaId"]
    assert {k: v for k, v in timer.items() if k != "serverNow"} == {
        "status": "IN_PROGRESS",
        "plannedTotalSeconds": 1800,
        "totalElapsedSeconds": 0,
        "pausedTotalSeconds": 0,
        "currentAgendaId": first_id,
        "agendaAllocatedSeconds": 600,
        "agendaElapsedSeconds": 0,
    }
    server_now = datetime.fromisoformat(timer["serverNow"])
    assert timer["serverNow"].endswith("+09:00")
    assert datetime.fromisoformat(data["invite"]["expiresAt"]) - server_now == timedelta(days=14)

    assert [
        (a["orderIndex"], a["title"], a["keywords"], a["allocatedSeconds"], a["status"]) for a in data["agendas"]
    ] == [
        (0, "지난주 캠페인 성과 리뷰", ["CTR", "전환율"], 600, "IN_PROGRESS"),
        (1, "4분기 예산 검토", ["예산", "광고비"], 720, "PENDING"),
        (2, "다음 주 액션 아이템", ["담당자", "일정"], 480, "PENDING"),
    ]
    assert data["host"]["name"] == "진행자" and data["host"]["role"] == "HOST"

    caller = read_token(data["accessToken"], code)
    assert caller is not None and caller.is_host and caller.participant_id == data["host"]["participantId"]

    # DB: 복구 키는 해시만, 첫 안건만 녹음 시작 위치 0
    meeting = await session.get(Meeting, code)
    assert meeting is not None
    key = data["hostRecoveryKey"]
    assert RECOVERY_KEY.match(key)
    assert key not in meeting.host_recovery_key_hash
    assert bcrypt.checkpw(key.encode(), meeting.host_recovery_key_hash.encode())
    assert (meeting.planned_total_minutes, meeting.target_total_minutes, meeting.attendee_count) == (30, 30, 4)
    assert meeting.started_at == meeting.consent_agreed_at
    rows = (await session.scalars(select(Agenda).where(Agenda.meeting_id == code).order_by(Agenda.order_index))).all()
    assert [r.recording_start_ms for r in rows] == [0, None, None]
    host = await session.get(Participant, data["host"]["participantId"])
    assert host is not None and host.password_hash is None


async def test_defaults_and_keyword_cleanup(client: httpx.AsyncClient) -> None:
    body = payload(
        title="  주간 회의  ",
        targetTotalMinutes=None,
        agendas=[agenda(title=" 리뷰 ", keywords=[" CTR", "CTR", "전환율 "], allocatedMinutes=7)],
    )
    del body["meetingMode"], body["attendeeCount"]
    res = await client.post("/api/v1/meetings", json=body)
    assert res.status_code == 201
    data = res.json()["data"]
    assert (data["title"], data["meetingMode"]) == ("주간 회의", "IN_PERSON")
    assert data["agendas"][0]["title"] == "리뷰"
    assert data["agendas"][0]["keywords"] == ["CTR", "전환율"]
    assert data["timer"]["plannedTotalSeconds"] == 420


@pytest.mark.parametrize(
    ("overrides", "message", "field"),
    [
        ({"title": " 회 "}, TITLE_MESSAGE, "title"),
        ({"title": "가" * 31}, TITLE_MESSAGE, "title"),
        ({"targetTotalMinutes": 7}, VALIDATION_MESSAGE, "targetTotalMinutes"),
        ({"targetTotalMinutes": 185}, VALIDATION_MESSAGE, "targetTotalMinutes"),
        ({"meetingMode": "HYBRID"}, VALIDATION_MESSAGE, "meetingMode"),
        ({"attendeeCount": 0}, VALIDATION_MESSAGE, "attendeeCount"),
        ({"attendeeCount": 21}, VALIDATION_MESSAGE, "attendeeCount"),
        ({"targetTotalMinutes": None, "agendas": []}, VALIDATION_MESSAGE, "agendas"),
        ({"targetTotalMinutes": None, "agendas": [agenda()] * 6}, AGENDA_LIMIT_MESSAGE, "agendas"),
        (
            {"targetTotalMinutes": None, "agendas": [agenda(), agenda(title="   ")]},
            AGENDA_TITLE_MESSAGE,
            "agendas[1].title",
        ),
        ({"targetTotalMinutes": None, "agendas": [agenda(title="가" * 21)]}, AGENDA_TITLE_MESSAGE, "agendas[0].title"),
        (
            {"targetTotalMinutes": None, "agendas": [agenda(keywords=["a", "b", "c", "d", "e", "f"])]},
            KEYWORD_LIMIT_MESSAGE,
            "agendas[0].keywords",
        ),
        (
            {"targetTotalMinutes": None, "agendas": [agenda(keywords=["일곱글자키워드"])]},
            VALIDATION_MESSAGE,
            "agendas[0].keywords",
        ),
        (
            {"targetTotalMinutes": None, "agendas": [agenda(keywords=["  "])]},
            VALIDATION_MESSAGE,
            "agendas[0].keywords",
        ),
        (
            {"targetTotalMinutes": None, "agendas": [agenda(allocatedMinutes=0)]},
            ALLOCATED_MESSAGE,
            "agendas[0].allocatedMinutes",
        ),
        (
            {"targetTotalMinutes": None, "agendas": [agenda(allocatedMinutes="10")]},
            VALIDATION_MESSAGE,
            "agendas[0].allocatedMinutes",
        ),
        (
            {"targetTotalMinutes": None, "agendas": [agenda(allocatedMinutes=100), agenda(allocatedMinutes=81)]},
            VALIDATION_MESSAGE,
            "agendas",
        ),
    ],
)
async def test_validation_errors(
    client: httpx.AsyncClient, session: AsyncSession, overrides: dict[str, Any], message: str, field: str
) -> None:
    res = await client.post("/api/v1/meetings", json=payload(**overrides))
    assert res.status_code == 400
    assert res.json() == {
        "success": False,
        "data": None,
        "error": {"code": "VALIDATION_ERROR", "message": message, "field": field},
    }
    assert await meeting_count(session) == 0


@pytest.mark.parametrize("consent", [False, None, "true", 1])
async def test_consent_required(client: httpx.AsyncClient, consent: Any) -> None:
    res = await client.post("/api/v1/meetings", json=payload(consentAgreed=consent))
    assert res.status_code == 400
    assert res.json()["error"] == {"code": "CONSENT_REQUIRED", "message": "음성 수집 및 AI 분석에 동의해주세요."}


async def test_consent_missing(client: httpx.AsyncClient) -> None:
    body = payload()
    del body["consentAgreed"]
    res = await client.post("/api/v1/meetings", json=body)
    assert res.json()["error"]["code"] == "CONSENT_REQUIRED"


async def test_agenda_time_mismatch(client: httpx.AsyncClient, session: AsyncSession) -> None:
    res = await client.post("/api/v1/meetings", json=payload(targetTotalMinutes=35))
    assert res.status_code == 400
    assert res.json()["error"] == {"code": "AGENDA_TIME_MISMATCH", "message": "안건 시간 합계가 목표 시간과 달라요."}
    assert await meeting_count(session) == 0


async def test_same_idempotency_key_creates_one_meeting(client: httpx.AsyncClient, session: AsyncSession) -> None:
    headers = {"Idempotency-Key": "8f3c0d6e-2b1a-4c55-9d1e-7a6b5c4d3e2f"}
    first, second = await asyncio.gather(
        client.post("/api/v1/meetings", json=payload(), headers=headers),
        client.post("/api/v1/meetings", json=payload(title="다른 제목"), headers=headers),
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert await meeting_count(session) == 1

    other = await client.post("/api/v1/meetings", json=payload(), headers={"Idempotency-Key": "another-key"})
    assert other.json()["data"]["meetingId"] != first.json()["data"]["meetingId"]
    assert await meeting_count(session) == 2


async def test_failed_request_is_not_cached(client: httpx.AsyncClient, session: AsyncSession) -> None:
    headers = {"Idempotency-Key": "retry-after-fix"}
    bad = await client.post("/api/v1/meetings", json=payload(targetTotalMinutes=35), headers=headers)
    assert bad.status_code == 400
    good = await client.post("/api/v1/meetings", json=payload(), headers=headers)
    assert good.status_code == 201
    assert await meeting_count(session) == 1


def test_idempotency_cache_expires_after_ttl() -> None:
    cache = IdempotencyCache(ttl_seconds=600)
    cache.put("k", "first", now=0)
    assert cache.get("k", now=599) == "first"
    assert cache.get("k", now=600) is None


async def test_meeting_code_collision_retries(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = iter(["AAAAAA", "AAAAAA", "BBBBBB"])
    monkeypatch.setattr(codes, "new_meeting_code", lambda: next(generated))

    first = await client.post("/api/v1/meetings", json=payload())
    second = await client.post("/api/v1/meetings", json=payload())
    assert first.json()["data"]["meetingId"] == "AAAAAA"
    assert second.json()["data"]["meetingId"] == "BBBBBB"
    assert await meeting_count(session) == 2


def test_generated_codes_use_the_alphabet() -> None:
    for _ in range(200):
        assert CODE.match(codes.new_meeting_code())
        assert RECOVERY_KEY.match(codes.new_recovery_key())
