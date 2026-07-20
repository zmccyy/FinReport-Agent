"""AI service exception hierarchy."""


class AiException(Exception):
    """Base exception for recoverable AI-service processing failures."""


class InvalidTaskMessageException(AiException):
    """Raised when a broker message violates the task contract."""
