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
    # M2.04 ModelHub — local model paths (relative to repo root or absolute).
    model_7b_path: str = "models/Qwen2.5-7B-Instruct-GPTQ-Int4"
    model_15b_path: str = "models/Qwen2.5-1.5B-Instruct"
    model_embed_path: str = "models/bge-small-zh-v1.5"
    # Inference SLA (spec §3.7 / §12.1).
    model_load_timeout_seconds: int = 300
    model_generate_timeout_seconds: int = 60
    model_max_new_tokens: int = 1024
    model_quant_7b: str = "gptq-int4"
    model_quant_15b: str = "nf4"
    # M2.05 model_lock + VRAM scheduler (spec §3.9 / §5.4.1).
    redis_url: str = "redis://localhost:6379/0"
    model_lock_ttl_seconds: int = 300
    model_lock_retry_seconds: float = 2.0
    vram_idle_threshold_seconds: int = 600
