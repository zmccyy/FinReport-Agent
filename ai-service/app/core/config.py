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
    # M4.10 报表页过滤（StatementPageFilter）：锚点正则命中「合并/母公司
    # +三表标题」后，向后 window 页内金额密度达标的页才做表格识别。
    # 背景：全页 PP-Structure 143 页 >18min；纯关键词仍命中附注密集页，
    # RT-DETR-L 在多表附注页内存膨胀导致容器 OOM 重启循环。
    parser_table_anchor_pattern: str = "(合并|母公司)(资产负债表|利润表|现金流量表)"
    parser_table_anchor_window: int = 4
    parser_table_amount_threshold: int = 15
    # M4.10 页面渲染 DPI：表格识别/OCR 的光栅化分辨率。默认 200（质量优先）；
    # 内存受限环境（如 8GB 笔记本 Docker VM）整页大表在 200 DPI 下
    # RT-DETR-L 单元格检测会 OOM，可降至 150（像素量 ~56%）。
    parser_render_dpi: int = 200
    # M4.10 Paddle 推理 oneDNN(MKLDNN) 开关，默认关闭：paddlepaddle 3.3.1
    # 在部分 CPU 上经 oneDNN 执行检测模型会抛
    # ``ConvertPirAttribute2RuntimeAttribute not support``（真实 E2E 发现）。
    # 关闭后走原生 kernel，稳定性优先；性能敏感的环境可显式置 true。
    parser_enable_mkldnn: bool = False
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
    # M4.10：16384 max_tokens 下 reasoning + 全量 JSON 输出实测 2-4 分钟，
    # 60s 必超时（ReadTimeout），提到 300s（抽取质量优先于单步 SLA）。
    model_generate_timeout_seconds: int = 300
    # M4.10：reasoning 模型（如 .env 配置的 deepseek-v4-flash）会把 token
    # 预算先花在 reasoning_content 上，低上限下 content 为空或截断
    # （finish_reason=length）。select_table 跨页拼接后单表 ~5KB HTML，
    # 全量抽取（本期+上期，~40 科目）JSON 输出 ~3-4k，8192 会被
    # reasoning 吃光（真实 E2E 实测 content empty），提到 16384。
    model_max_new_tokens: int = 16384
    # M4.02 DeepSeek API (OpenAI 兼容协议, 2026-08-16 起本地训练取消).
    llm_api_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_api_model: str = "deepseek-chat"
    llm_api_max_retries: int = 2
    llm_api_retry_base_delay_seconds: float = 1.0
