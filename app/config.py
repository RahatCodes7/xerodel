from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BOT_TOKEN: str = ""
    BOT_USERNAME: str = ""
    PUBLIC_CHANNEL_ID: str = ""
    PRIVATE_CHANNEL_ID: str = ""
    ADMIN_IDS: str = ""

    WEBHOOK_SECRET: str = "change-me-webhook-secret"
    SESSION_SECRET: str = "change-me-session-secret"

    BASE_URL: str = "http://localhost:8000"
    USE_POLLING: bool = False

    ORDER_EXPIRY_MINUTES: int = 30
    DATABASE_PATH: str = "data/app.db"
    PROOF_DIR: str = "data/proofs"

    PORT: int = 8000
    SITE_NAME: str = "Xerodel"
    SITE_CURRENCY: str = "৳"

    BKASH_NUMBER: str = ""
    NAGAD_NUMBER: str = ""
    PAYMENT_INSTRUCTIONS: str = ""

    SEED_DEMO: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
