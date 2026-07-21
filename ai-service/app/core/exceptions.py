"""AI service exception hierarchy."""


class AiException(Exception):
    """Base exception for recoverable AI-service processing failures."""


class InvalidTaskMessageException(AiException):
    """Raised when a broker message violates the task contract."""


class ModelLoadException(AiException):
    """Raised when a model cannot be loaded (OOM, missing weights, deps, etc.)."""


class InferenceTimeoutException(AiException):
    """Raised when model inference exceeds the configured SLA timeout."""


class LockAcquisitionException(AiException):
    """Raised when a Redis distributed lock cannot be acquired due to errors."""


class ModelLockBusyException(AiException):
    """Raised when the model_lock is held by another worker (spec §3.9)."""
