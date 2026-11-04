# 칼퇴 backend

실시간 회의 페이스메이커 **칼퇴**의 FastAPI 서버입니다. 회의·안건 관리, 회의룸 실시간 동기화(WebSocket), STT, AI(사담 판단·요약)를 담당합니다.

## 구조

```
app/
  main.py         앱 생성, CORS, 라우터 연결
  config.py       환경 변수 (.env)
  api/            HTTP API
  realtime/       회의룸 WebSocket — 같은 회의 참여자에게 이벤트 전달
  stt/            STT 엔진 연동 — 결과는 Utterance 로 통일
    rtzr.py       리턴제로 스트리밍: 회의 중 실시간 자막·사담 판단 입력
    clova.py      CLOVA Speech 장문: 회의 후 녹음 화자 분리
tests/
```

STT 엔진 선택 근거는 PoC 1 결과 보고서(노션)를 참고하세요.

## 시작하기

[uv](https://docs.astral.sh/uv/)가 필요합니다. Python 3.13 은 uv 가 설치합니다.

```bash
uv sync                      # 의존성 설치
cp .env.example .env         # 값 채우기
uv run uvicorn app.main:app --reload
```

- API 문서: http://localhost:8000/docs
- 상태 확인: http://localhost:8000/health

## Docker

```bash
docker build -t kaltoe-backend .
docker run --rm -p 8000:8000 --env-file .env kaltoe-backend
```

- 이미지에는 `app/` 과 의존성만 들어가고, `.env` 는 실행할 때 `--env-file` 로 넘깁니다.
- 회의룸 연결을 메모리에서 관리하므로 **컨테이너 1개, 워커 1개**로만 띄웁니다. 여러 개로 늘리면 같은 회의 참가자끼리 이벤트가 전달되지 않습니다.
- HTTPS(`wss`)는 앞단 프록시(Caddy 등)가 맡습니다. 브라우저 마이크는 HTTPS 에서만 동작합니다.
- 서버에서는 `-p 127.0.0.1:8000:8000` 으로 띄워 8000 포트를 외부에 열지 않습니다. 앱이 프록시 헤더를 믿도록 설정돼 있어서, 프록시를 거치지 않는 접근을 막아야 합니다.

## 검사

CI(`.github/workflows/ci.yml`)가 PR마다 아래를 실행합니다. 올리기 전에 로컬에서 먼저 돌려 주세요.

```bash
uv run ruff check .
uv run ruff format --check .   # 고치려면 uv run ruff format .
uv run mypy
uv run pytest
```

## 지키는 것

- **main 에는 PR 로만.** 리뷰어 1명 이상 승인과 CI 통과가 있어야 병합됩니다 (브랜치 보호로 강제).
- **Squash merge 만 사용**하므로 PR 제목이 main 의 커밋 메시지가 됩니다. `feat: 회의 생성 API 추가` 형식을 지켜 주세요.
- **비밀값은 `.env` 에만.** 공개 저장소이므로 API 키, 회의 녹음, 실제 회의 대화 내용을 커밋하지 않습니다.
- **API·실시간 이벤트 형식을 바꾸면** API 명세를 먼저 갱신하고 프론트에 공유합니다.

브랜치·커밋 규칙 전체는 [CONTRIBUTING.md](https://github.com/kaltoe/.github/blob/main/CONTRIBUTING.md)에 있습니다.
