# 칼퇴 backend

실시간 회의 페이스메이커 **칼퇴**의 FastAPI 서버입니다. 회의·안건 관리, 회의룸 실시간 동기화(WebSocket), STT, AI(사담 판단·요약)를 담당합니다.

## 구조

```
app/
  main.py         앱 생성, CORS, 라우터 연결
  config.py       환경 변수 (.env)
  api/            HTTP API 공통 규약 — 응답 형식, 에러 코드, 토큰, 요청 제한
  meetings/       회의 API — 타이머 계산(timer.py), 회의 생성(create.py), 입장 정보(entry.py)
  db/             DB 모델(ERD 명세서 기준), 세션, 마이그레이션(migrations/)
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

## DB

PostgreSQL 17 을 씁니다. 로컬에서는 Docker 로 띄우고 마이그레이션을 적용합니다.

```bash
docker run -d --name kaltoe-db -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=kaltoe -e POSTGRES_PASSWORD=kaltoe -e POSTGRES_DB=kaltoe postgres:17
uv run alembic upgrade head
```

- 5432 포트를 이미 쓰고 있으면 `-p 127.0.0.1:5433:5432` 처럼 바꾸고 `.env` 의 `DATABASE_URL` 포트도 맞춥니다.
- 모델(`app/db/models.py`)을 바꾸면 `uv run alembic revision --autogenerate -m "설명"` 으로 마이그레이션을 만들고, 생성된 파일을 확인한 뒤 함께 커밋합니다.
- 테이블 기준은 노션 ERD 명세서의 테이블 표입니다.
- DB 테스트는 테스트마다 롤백합니다. 로컬에 DB가 없으면 DB 테스트만 건너뛰고, CI 에서는 Postgres 서비스로 항상 실행합니다.

## Docker

```bash
docker build -t kaltoe-backend .
docker run --rm -p 8000:8000 --env-file .env kaltoe-backend
```

- 이미지에는 `app/`, `alembic.ini` 와 의존성만 들어가고, `.env` 는 실행할 때 `--env-file` 로 넘깁니다.
- 마이그레이션은 컨테이너 안에서 `alembic upgrade head` 로 적용합니다.
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

- **main 에는 PR 로만.** CI 통과가 있어야 병합됩니다 (브랜치 보호로 강제). 리뷰 승인은 필수가 아니며 작성자가 직접 병합합니다.
- **Squash merge 만 사용**하므로 PR 제목이 main 의 커밋 메시지가 됩니다. `feat: 회의 생성 API 추가` 형식을 지켜 주세요.
- **비밀값은 `.env` 에만.** 공개 저장소이므로 API 키, 회의 녹음, 실제 회의 대화 내용을 커밋하지 않습니다.
- **API·실시간 이벤트 형식을 바꾸면** API 명세를 먼저 갱신하고 프론트에 공유합니다.

브랜치·커밋 규칙 전체는 [CONTRIBUTING.md](https://github.com/kaltoe/.github/blob/main/CONTRIBUTING.md)에 있습니다.
