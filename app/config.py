"""환경 변수 설정. 값은 .env 또는 실행 환경에서 읽는다."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    # 쉼표로 구분한 프론트 주소 (예: http://localhost:3000,https://kaltoe.app)
    cors_origins: str = "http://localhost:3000"

    # CLOVA Speech 장문 인식 — 회의 후 화자 분리
    clova_long_invoke_url: str = ""
    clova_long_secret: str = ""

    # 리턴제로 — 회의 중 실시간 STT
    rtzr_client_id: str = ""
    rtzr_client_secret: str = ""

    # DB · 인증
    database_url: str = "postgresql://kaltoe:kaltoe@localhost:5432/kaltoe"
    jwt_secret: str = ""

    # 녹음 저장 — S3 는 EC2 인스턴스 역할로 인증하므로 액세스 키를 두지 않는다
    s3_bucket: str = "kaltoe-recordings"
    aws_region: str = "ap-northeast-2"
    # 회의 중 기기별 WAV · 소리 구간 json 을 쌓는 디렉터리 (서버에서는 호스트 볼륨)
    recordings_dir: str = "./data/recordings"

    # OpenAI — 사담 판정 · 요약
    openai_api_key: str = ""
    offtopic_interval_seconds: int = Field(default=10, ge=10, le=15)

    # CLOVA 결과 콜백 (INT-01)
    clova_callback_secret: str = ""
    clova_callback_url: str = "https://api.wacaw.shop/api/v1/internal/clova/callback"

    # 초대 …/m/{code} · 공유 …/s/{shareId} 링크의 앞부분
    frontend_base_url: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
