"""CLOVA Speech 장문 인식 — 회의 후 녹음 파일 화자 분리.

PoC 1: 실제 회의 녹음에서 화자 정확도 99%, CER 2.2%. 참석 인원을 화자 수로 지정하면 정확도가 유지됐다.
"""

import json
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.stt.models import Utterance


class ClovaError(RuntimeError):
    pass


def build_params(speakers: int | None) -> dict[str, Any]:
    diarization: dict[str, Any] = {"enable": True}
    if speakers:
        diarization.update({"speakerCountMin": speakers, "speakerCountMax": speakers})
    return {"language": "ko-KR", "completion": "sync", "diarization": diarization, "fullText": True}


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


async def diarize(audio_path: Path, settings: Settings, speakers: int | None = None) -> list[Utterance]:
    """녹음 파일을 올려 화자가 붙은 발화 목록을 받는다. speakers 는 참석 인원."""
    async with httpx.AsyncClient(timeout=600) as client:
        with audio_path.open("rb") as f:
            resp = await client.post(
                f"{settings.clova_long_invoke_url.rstrip('/')}/recognizer/upload",
                headers={"X-CLOVASPEECH-API-KEY": settings.clova_long_secret},
                files={"media": f},
                data={"params": json.dumps(build_params(speakers))},
            )
    resp.raise_for_status()
    return parse_response(resp.json())
