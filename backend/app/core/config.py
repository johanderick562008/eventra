"""Application configuration, loaded from environment variables (and `.env` in development)."""

from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

INSECURE_DEV_SECRET = "insecure-development-secret-do-not-use-in-production"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_name: str = "Eventra Ticketing API"
    app_version: str = "1.0.0"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql+psycopg://eventra:eventra@localhost:5433/eventra"
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout_seconds: int = Field(default=30, ge=1)
    db_statement_timeout_ms: int = Field(default=15_000, ge=0)
    db_lock_timeout_ms: int = Field(default=10_000, ge=0)

    # Authentication
    jwt_secret_key: str = INSECURE_DEV_SECRET
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "eventra-api"
    access_token_expire_minutes: int = Field(default=30, ge=1, le=1440)
    refresh_token_expire_days: int = Field(default=14, ge=1, le=90)

    # HTTP
    frontend_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # Business rules
    booking_max_tickets_per_booking: int = Field(default=10, ge=1, le=100)
    booking_cancellation_cutoff_hours: int = Field(default=24, ge=0)

    # Optional features (0 disables)
    booking_rate_limit_per_minute: int = Field(default=5, ge=0)
    event_status_sweep_interval_seconds: int = Field(default=300, ge=0)

    @field_validator("frontend_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, value: str) -> str:
        # Hosting providers (Render, Railway, Heroku) hand out postgres:// URLs.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @model_validator(mode="after")
    def _validate_production(self) -> "Settings":
        if self.environment == "production":
            if self.jwt_secret_key == INSECURE_DEV_SECRET or len(self.jwt_secret_key) < 32:
                raise ValueError(
                    "JWT_SECRET_KEY must be a random value of at least 32 characters in production"
                )
            if "*" in self.frontend_origins:
                raise ValueError("FRONTEND_ORIGINS must list explicit origins in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
