from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "AI Trends"
    database_url: str
    bot_token: str
    jwt_secret: str
    jwt_expires_seconds: int = 3600 * 24 * 7
    kie_api_key: str
    kie_api_base: str = "https://api.kie.ai"
    kie_file_upload_base: str = "https://kieai.redpandaai.co"
    kie_enable_fallback: bool = False
    kie_callback_url: str | None = None
    cors_origins: str = "*"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

