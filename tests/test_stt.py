import pytest

from app.stt import clova, rtzr


def test_clova_params_fix_speaker_count() -> None:
    params = clova.build_params(speakers=3, callback="https://api/cb", userdata={"meetingId": "K7Q2MX"})
    assert params["diarization"] == {"enable": True, "speakerCountMin": 3, "speakerCountMax": 3}
    # 비동기 + 콜백, 한국어, 단어 위치·전체 텍스트
    assert (params["completion"], params["callback"], params["userdata"]) == (
        "async",
        "https://api/cb",
        {"meetingId": "K7Q2MX"},
    )
    assert (params["language"], params["wordAlignment"], params["fullText"]) == ("ko-KR", True, True)
    assert clova.build_params(speakers=None)["diarization"] == {"enable": True}  # attendeeCount 가 없으면 자동


def test_clova_parse_segments() -> None:
    body = {
        "result": "COMPLETED",
        "segments": [
            {"start": 0, "end": 4200, "text": "첫 안건 시작할게요", "speaker": {"label": "1"}},
            {"start": 4500, "end": 7000, "text": "네 좋아요", "speaker": {"label": "2"}},
        ],
    }
    utterances = clova.parse_response(body)
    assert [(u.speaker, u.text) for u in utterances] == [("1", "첫 안건 시작할게요"), ("2", "네 좋아요")]


def test_clova_failed_result_raises() -> None:
    with pytest.raises(clova.ClovaError):
        clova.parse_response({"result": "FAILED", "message": "무료 인식 시간 초과"})


def test_rtzr_stream_uses_meeting_domain() -> None:
    query = rtzr.stream_query()
    assert "domain=MEETING" in query
    assert "model_name=sommers_ko" in query


def test_rtzr_parse_final_only() -> None:
    partial = {"seq": 0, "start_at": 1000, "duration": 0, "final": False, "alternatives": [{"text": "블로"}]}
    final = {"seq": 0, "start_at": 1000, "duration": 2500, "final": True, "alternatives": [{"text": "블로그 예산"}]}
    assert rtzr.parse_final(partial) is None
    utterance = rtzr.parse_final(final)
    assert utterance is not None
    assert (utterance.start_ms, utterance.end_ms, utterance.text) == (1000, 3500, "블로그 예산")
