"""Project logging helpers."""

import logging


def get_logger(name: str) -> logging.Logger:
    """Return the project logger for a module.

    Args:
        name: Fully-qualified module name.

    Returns:
        Configured standard-library logger.
    """
    return logging.getLogger(name)
