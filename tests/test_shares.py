"""공유 뷰(#32) API-16·17·18. 토큰 없이 shareId 로 연다."""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import to_kst_iso
from app.db.models import Agenda, Meeting, ShareLink
from app.meetings.recording import s3_client
from tests.test_agendas import T0, at, bearer, error
from tests.test_recording import set_recording
from tests.test_result import finished, warn
from tests.test_transcripts import setup as with_utterances

DAY = 24 * 3600
ENDED_AT = 1270  # finished() 가 회의를 끝내는 시각(초)


@pytest.fixture(autouse=True)
def fake_aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """presigned URL 은 서명만 하므로 가짜 자격 증명으로 실제 boto3 를 쓴다(S3 에 접속하지 않음)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTTESTTESTTEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    s3_client.cache_clear()
    yield
    s3_client.cache_clear()


async def share_id(client: httpx.AsyncClient, created: dict[str, Any]) -> str:
    response = await client.post(
        f"/api/v1/meetings/{created['meetingId']}/share", headers=bearer(created["accessToken"])
    )
    return str(response.json()["data"]["shareId"])


async def as_member(client: httpx.AsyncClient, created: dict[str, Any], path: str) -> dict[str, Any]:
    url = f"/api/v1/meetings/{created['meetingId']}/{path}"
    response = await client.get(url, headers=bearer(created["accessToken"]))
    return dict(response.json()["data"])


async def test_shared_meeting_is_result_with_summary_lines(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, (review, budget, _) = await finished(client, session, monkeypatch)
    code = created["meetingId"]
    await warn(session, code, review)
    sid = await share_id(client, created)

    # 요약이 아직이면 summaryLines 는 빈 배열
    data = (await client.get(f"/api/v1/shares/{sid}")).json()["data"]
    assert data["summaryStatus"] == "PROCESSING"
    assert [a["summaryLines"] for a in data["agendas"]] == [[], []]

    meeting = await session.get(Meeting, code)
    assert meeting is not None
    meeting.summary_status = "DONE"
    for agenda in await session.scalars(select(Agenda).where(Agenda.meeting_id == code)):
        agenda.summary_lines = ["한 줄이에요.", "두 줄이에요.", "세 줄이에요."] if agenda.id == review else []
    await session.flush()

    data = (await client.get(f"/api/v1/shares/{sid}")).json()["data"]  # 토큰 없이 연다
    result = await as_member(client, created, "result")
    # API-10 과 같은 값에서 회의 코드·종료 주체·녹음 구간·상태 값은 빼고, 안건별 요약과 링크 만료를 더한다
    assert {k: data[k] for k in ("title", "participantCount", "startedAt", "endedAt", "time")} == {
        k: result[k] for k in ("title", "participantCount", "startedAt", "endedAt", "time")
    }
    expected_agendas = [
        {k: v for k, v in a.items() if k not in ("recordingStartMs", "recordingEndMs")}
        | {"summaryLines": ["한 줄이에요.", "두 줄이에요.", "세 줄이에요."] if a["agendaId"] == review else []}
        for a in result["agendas"]
    ]
    assert data["agendas"] == expected_agendas
    assert [a["agendaId"] for a in data["agendas"]] == [review, budget]  # 끝내지 못한 안건은 빠진다
    assert data["agendas"][0]["offtopicCount"] == 1
    assert data["summaryStatus"] == "DONE"
    link = await session.scalar(select(ShareLink).where(ShareLink.share_id == sid))
    assert link is not None
    assert data["expiresAt"] == to_kst_iso(link.expires_at)  # API-15 가 준 만료 시각 그대로
    assert set(data) == {
        "title",
        "participantCount",
        "startedAt",
        "endedAt",
        "time",
        "agendas",
        "summaryStatus",
        "expiresAt",
    }


async def test_shared_transcripts_are_api14_auto_without_utterance_id(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    created = await with_utterances(session, client)
    await client.post(f"/api/v1/meetings/{created['meetingId']}/end", headers=bearer(created["accessToken"]))
    sid = await share_id(client, created)

    for postprocess in ("PROCESSING", "DONE"):  # 후처리가 끝나면 AUTO 가 POSTPROCESS 를 고른다
        meeting = await session.get(Meeting, created["meetingId"])
        assert meeting is not None
        meeting.postprocess_status = postprocess
        await session.flush()
        shared = (await client.get(f"/api/v1/shares/{sid}/transcripts")).json()["data"]
        full = await as_member(client, created, "transcripts")
        for group in full["groups"]:
            for item in group["items"]:
                del item["utteranceId"]
        assert shared == full
        assert shared["source"] == ("POSTPROCESS" if postprocess == "DONE" else "REALTIME")


async def test_shared_recording_matches_api13_and_expires_with_recording(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, _ = await finished(client, session, monkeypatch)
    code = created["meetingId"]
    sid = await share_id(client, created)

    processing = (await client.get(f"/api/v1/shares/{sid}/recording")).json()["data"]
    assert (processing["status"], processing["url"]) == ("PROCESSING", None)

    await set_recording(session, code, "READY", f"recordings/{code}/merged.m4a")
    shared = (await client.get(f"/api/v1/shares/{sid}/recording")).json()["data"]
    own = await as_member(client, created, "recording")
    assert shared == own  # 같은 시각에 같은 파일을 서명한다
    assert f"/recordings/{code}/merged.m4a" in shared["url"]

    # 결제로 링크를 다시 연 경우처럼 링크가 살아 있어도, 녹음 보관(종료 + 14일)이 지나면 녹음만 410
    link = await session.scalar(select(ShareLink).where(ShareLink.share_id == sid))
    assert link is not None
    link.expires_at = T0 + timedelta(days=30)
    await session.flush()
    at(monkeypatch, ENDED_AT + 14 * DAY)
    assert error(await client.get(f"/api/v1/shares/{sid}/recording")) == (410, "RECORDING_EXPIRED")
    assert (await client.get(f"/api/v1/shares/{sid}")).status_code == 200  # 회의록은 그대로


@pytest.mark.parametrize("path", ["", "/transcripts", "/recording"])
async def test_unknown_link_is_404_and_expired_link_is_410(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    assert error(await client.get(f"/api/v1/shares/nope{path}")) == (404, "SHARE_NOT_FOUND")

    created, _ = await finished(client, session, monkeypatch)
    sid = await share_id(client, created)
    at(monkeypatch, ENDED_AT + 7 * DAY - 1)  # 만료 1초 전
    assert (await client.get(f"/api/v1/shares/{sid}{path}")).status_code == 200
    at(monkeypatch, ENDED_AT + 7 * DAY)  # 회의 종료 + 7일(API-15)
    response = await client.get(f"/api/v1/shares/{sid}{path}")
    assert error(response) == (410, "SHARE_EXPIRED")
    assert response.json()["error"]["message"] == "만료되었거나 잘못된 링크예요."  # 404 와 같은 문구
