from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import to_kst_iso
from app.db.models import Meeting
from tests.test_agendas import T0, at, bearer, create, error, start_at_t0


@pytest.fixture(autouse=True)
def fake_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """presigned URL 은 서버에서 서명만 하므로 가짜 자격 증명으로 실제 boto3 를 쓴다(S3 에 접속하지 않음)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTTESTTESTTEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


async def ended(client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    created = await create(client)
    await start_at_t0(session, created["meetingId"])
    at(monkeypatch, 1600)
    await client.post(f"/api/v1/meetings/{created['meetingId']}/end", headers=bearer(created["accessToken"]))
    return created


async def recording(client: httpx.AsyncClient, created: dict[str, Any]) -> httpx.Response:
    return await client.get(
        f"/api/v1/meetings/{created['meetingId']}/recording", headers=bearer(created["accessToken"])
    )


async def set_recording(session: AsyncSession, code: str, status: str, key: str | None = None) -> None:
    meeting = await session.get(Meeting, code)
    assert meeting is not None
    await session.refresh(meeting)
    meeting.recording_status, meeting.recording_m4a_s3_key, meeting.recording_duration_ms = status, key, 1_600_000
    await session.flush()


async def test_ready_gives_one_hour_presigned_url(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await ended(client, session, monkeypatch)
    code = created["meetingId"]
    await set_recording(session, code, "READY", f"recordings/{code}/merged.m4a")
    at(monkeypatch, 2000)

    data = (await recording(client, created)).json()["data"]
    assert (data["status"], data["durationMs"], data["mimeType"]) == ("READY", 1_600_000, "audio/mp4")
    assert data["urlExpiresAt"] == to_kst_iso(T0 + timedelta(seconds=2000) + timedelta(hours=1))
    url = urlparse(data["url"])
    assert url.hostname is not None and url.hostname.startswith("kaltoe-recordings.s3")
    assert url.path == f"/recordings/{code}/merged.m4a"
    query = parse_qs(url.query)
    assert query["X-Amz-Expires"] == ["3600"]  # 1시간
    assert query["response-content-type"] == ["audio/mp4"]


@pytest.mark.parametrize("status", ["PROCESSING", "FAILED"])
async def test_not_ready_has_no_url(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    created = await ended(client, session, monkeypatch)
    await set_recording(session, created["meetingId"], status)
    res = await recording(client, created)
    assert (res.status_code, res.json()["data"]) == (
        200,
        {"status": status, "url": None, "urlExpiresAt": None, "durationMs": None, "mimeType": None},
    )


async def test_expires_14_days_after_end(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await ended(client, session, monkeypatch)  # T0 + 1600초에 끝남
    code = created["meetingId"]
    await set_recording(session, code, "READY", f"recordings/{code}/merged.m4a")
    end = 1600 + 14 * 24 * 3600
    at(monkeypatch, end - 1)
    assert (await recording(client, created)).status_code == 200
    at(monkeypatch, end)  # S3 를 확인하지 않고 410
    assert error(await recording(client, created)) == (410, "RECORDING_EXPIRED")


async def test_live_meeting_and_auth(client: httpx.AsyncClient) -> None:
    created = await create(client)
    data = (await recording(client, created)).json()["data"]
    assert data["status"] == "PROCESSING" and data["url"] is None  # 아직 끝나지 않은 회의
    assert error(await client.get(f"/api/v1/meetings/{created['meetingId']}/recording")) == (401, "UNAUTHORIZED")
