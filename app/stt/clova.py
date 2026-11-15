"""CLOVA Speech 장문 인식 — 회의 후 녹음 파일 화자 분리.

PoC 1: 실제 회의 녹음에서 화자 정확도 99%, CER 2.2%. 참석 인원을 화자 수로 지정하면 정확도가 유지됐다.
비동기(async)로 올리고 결과는 콜백(INT-01)으로 받는다.
"""

import json
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.stt.models import Utterance

UPLOAD_TIMEOUT_SECONDS = 600


class ClovaError(RuntimeError):
    pass


def build_params(speakers: int | None, callback: str = "", userdata: dict[str, Any] | None = None) -> dict[str, Any]:
    """speakers 는 셋업의 attendeeCount. 최소·최대를 모두 그 값으로, None 이면 자동."""
    diarization: dict[str, Any] = {"enable": True}
    if speakers:
        diarization.update({"speakerCountMin": speakers, "speakerCountMax": speakers})
    return {
        "language": "ko-KR",
        "completion": "async",
        "callback": callback,
        "userdata": userdata or {},
        "diarization": diarization,
        "wordAlignment": True,
        "fullText": True,
    }


def parse_response(body: dict[str, Any]) -> list[Utterance]:
    if body.get("result") not in (None, "COMPLETED"):
        raise ClovaError(f"{body.get('result')}: {body.get('message')}")
    return [
        Utterance(
            start_ms=seg["start"],
            end_ms=seg["end"],
            text=seg["text"],
            speaker=str((seg.get("speaker") or seg.get("diarization") or {}).get("label", "")) or None,
        )
        for seg in body.get("segments", [])
    ]


async def request(audio_path: Path, settings: Settings, speakers: int | None, userdata: dict[str, Any]) -> None:
    """녹음 파일을 올려 인식을 맡긴다. 접수되면 돌아오고, 결과는 settings.clova_callback_url 로 온다."""
    params = build_params(speakers, settings.clova_callback_url, userdata)
    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT_SECONDS) as client:
        with audio_path.open("rb") as f:
            resp = await client.post(
                f"{settings.clova_long_invoke_url.rstrip('/')}/recognizer/upload",
                headers={"X-CLOVASPEECH-API-KEY": settings.clova_long_secret},
                files={"media": f},
                data={"params": json.dumps(params)},
            )
    resp.raise_for_status()
