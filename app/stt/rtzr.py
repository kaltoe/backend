"""리턴제로 스트리밍 STT — 회의 중 실시간 자막·사담 판단 입력.

PoC 1: sommers_ko + domain=MEETING 이 최선 (CER 28.4%, 첫 글자 약 2초, 확정 약 0.2초).
기본값 domain=CALL 은 CER 56.8%, whisper 는 느리고 없는 문장을 만들어서 쓰지 않는다.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect

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


async def transcribe_stream(audio: AsyncIterator[bytes], settings: Settings) -> AsyncIterator[dict[str, Any]]:
    """16kHz 16bit mono PCM 청크를 보내고, 엔진이 보내는 메시지(임시·확정 결과)를 그대로 내보낸다."""
    token = await get_token(settings)
    url = f"{STREAM_URL}?{stream_query()}"
    async with connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as ws:

        async def send() -> None:
            async for chunk in audio:
                await ws.send(chunk)
            await ws.send("EOS")

        sender = asyncio.create_task(send())
        try:
            async for raw in ws:
                yield json.loads(raw)
        finally:
            sender.cancel()
