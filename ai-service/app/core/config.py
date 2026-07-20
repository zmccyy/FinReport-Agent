"""Application configuration backed by environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the AI service."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    ai_service_port: int = 8000
    mq_consumer_enabled: bool = True
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "guest"
    rabbitmq_pass: str = "guest"
    rabbitmq_vhost: str = "/"
    rabbitmq_heartbeat: int = 30
    rabbitmq_reconnect_delay_seconds: float = 1.0
    log_level: str = "INFO"
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_upload_bucket: str = "finreport-uploads"
