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
    # M4.03 parse 中间产物桶（parsed/{taskId}.json，供 extract 步骤消费）。
    minio_artifact_bucket: str = "finreport-artifacts"
    # M4.03 表格识别开关：开启后 parse 挂载 PP-Structure TableRecognizer，
    # 扫描件 OCR 兜底仍保持关闭（SLA 退路，spec §12.1 PARSE < 90s）。
    parser_enable_table_recognition: bool = True
    # M4.05 L3 只读 MySQL（compose MYSQL_* 预留兑现；仅 SELECT，写入归 L2）。
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "finreport"
    mysql_password: str = "finreport"
    mysql_database: str = "finreport"
    # M4.07 本地 embedding 模型路径（bge-small-zh-v1.5，CPU）。
    model_embed_path: str = "models/bge-small-zh-v1.5"
    # Inference SLA (spec §3.7 / §12.1) — M4.08 后作用于 DeepSeek API 路由。
    # （model_load_timeout_seconds 已随 GPU 栈移除：API 路由无本地加载环节。）
    model_generate_timeout_seconds: int = 60
    model_max_new_tokens: int = 1024
    # M4.02 DeepSeek API (OpenAI 兼容协议, 2026-08-16 起本地训练取消).
    llm_api_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_api_model: str = "deepseek-chat"
    llm_api_max_retries: int = 2
    llm_api_retry_base_delay_seconds: float = 1.0
