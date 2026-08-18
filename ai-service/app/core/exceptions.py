"""AI service exception hierarchy (M4.08: model_lock 异常随 GPU 栈移除)."""


class AiException(Exception):
    """Base exception for recoverable AI-service processing failures."""


class InvalidTaskMessageException(AiException):
    """Raised when a broker message violates the task contract."""


class ModelLoadException(AiException):
    """Raised when a model backend cannot initialize (missing key, deps, etc.)."""


class InferenceTimeoutException(AiException):
    """Raised when model inference exceeds the configured SLA timeout."""
