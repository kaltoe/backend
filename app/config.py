"""환경 변수 설정. 값은 .env 또는 실행 환경에서 읽는다."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    # 쉼표로 구분한 프론트 주소 (예: http://localhost:3000,https://kaltoe.app)
    cors_origins: str = "http://localhost:3000"

    # PostgreSQL (asyncpg). 로컬 기본값은 README 의 docker run 명령과 같다
    database_url: str = "postgresql+asyncpg://kaltoe:kaltoe@localhost:5432/kaltoe"

    # CLOVA Speech 장문 인식 — 회의 후 화자 분리
    clova_long_invoke_url: str = ""
    clova_long_secret: str = ""

    # 리턴제로 — 회의 중 실시간 STT
    rtzr_client_id: str = ""
    rtzr_client_secret: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
