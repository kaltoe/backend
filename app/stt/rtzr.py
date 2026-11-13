"""리턴제로 스트리밍 STT — 회의 중 실시간 자막·사담 판단 입력.

PoC 1: sommers_ko + domain=MEETING 이 최선 (CER 28.4%, 첫 글자 약 2초, 확정 약 0.2초).
기본값 domain=CALL 은 CER 56.8%, whisper 는 느리고 없는 문장을 만들어서 쓰지 않는다.
"""

import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import ClientConnection, connect

from app.config import Settings
from app.stt.models import Utterance

API = "https://openapi.vito.ai/v1"
STREAM_URL = "wss://openapi.vito.ai/v1/transcribe:streaming"
SAMPLE_RATE = 16000


def stream_query() -> str:
    return urlencode(
        {
            "sample_rate": SAMPLE_RATE,
            "encoding": "LINEAR16",
            "model_name": "sommers_ko",
            "domain": "MEETING",
            "use_itn": "true",
            "use_disfluency_filter": "false",
            "use_punctuation": "true",
        }
    )


def parse_final(msg: dict[str, Any]) -> Utterance | None:
    """확정(final) 결과만 Utterance 로 바꾼다. 임시 결과는 None."""
    if not msg.get("final"):
        return None
    start = msg["start_at"]
    return Utterance(start_ms=start, end_ms=start + msg["duration"], text=msg["alternatives"][0]["text"])


async def get_token(settings: Settings) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{API}/authenticate",
            data={"client_id": settings.rtzr_client_id, "client_secret": settings.rtzr_client_secret},
        )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


class RtzrSession:
    """스트리밍 세션 하나. 16kHz 16bit mono PCM 을 넣고, 엔진 메시지(임시·확정 결과)를 results() 로 읽는다."""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws

    async def send(self, pcm: bytes) -> None:
        await self.ws.send(pcm)

    async def finalize(self) -> None:
        """진행 중인 문장을 확정한다. 연결은 그대로 두고 이어서 소리를 넣는다."""
        await self.ws.send(json.dumps({"type": "Finalize"}))

    async def close(self) -> None:
        """EOS. 남은 결과를 보낸 뒤 엔진이 연결을 닫으면 results() 가 끝난다."""
        await self.ws.send("EOS")

    async def results(self) -> AsyncIterator[dict[str, Any]]:
        async for raw in self.ws:
            yield json.loads(raw)


async def open_session(settings: Settings) -> RtzrSession:
    token = await get_token(settings)
    ws = await connect(f"{STREAM_URL}?{stream_query()}", additional_headers={"Authorization": f"Bearer {token}"})
    return RtzrSession(ws)
