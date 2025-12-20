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
    yookassa_shop_id: str | None = None
    yookassa_secret_key: str | None = None
    frontend_url: str = "https://iiapp-66742.web.app"
    bot_username: str | None = None  # Имя бота без @ (например: my_bot)
    direct_link_name: str = "app"  # Имя Direct Link (например: "app" для t.me/bot_username/app)
    # Webhooks для n8n - можно указать несколько через запятую или один общий
    n8n_webhook_urls: str | None = None  # Формат: "url1,url2,url3" или просто "url"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

