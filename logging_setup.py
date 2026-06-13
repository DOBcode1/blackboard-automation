"""
Shared logging configuration for blackboard-automation.

Usage:
    from logging_setup import get_logger
    logger = get_logger("app")
"""

import logging
import os
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler


def get_logger(name: str) -> logging.Logger:
    """Return a logger with stderr + dated file handlers.

    Idempotent: if the logger already has handlers, return it as-is.
    Creates logs/ directory on first call if it does not exist.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    os.makedirs("logs", exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S"
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    date_str = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join("logs", f"app-{date_str}.log")
    file_handler = TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    logger.setLevel(logging.DEBUG)

    return logger
