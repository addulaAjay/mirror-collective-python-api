"""
Centralized logging configuration
"""

import logging
import logging.config
import os
import sys
from typing import Any, Dict


def get_logging_config() -> Dict[str, Any]:
    """Get logging configuration based on environment"""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "detailed": {
                "format": (
                    "%(asctime)s - %(name)s - %(levelname)s - "
                    "%(message)s - [%(filename)s:%(lineno)d]"
                ),
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "simple": {"format": "%(levelname)s - %(message)s"},
            "json": {
                "format": (
                    '{"time":"%(asctime)s","name":"%(name)s",'
                    '"level":"%(levelname)s","message":"%(message)s",'
                    '"file":"%(filename)s","line":%(lineno)d}'
                ),
                "datefmt": "%Y-%m-%dT%H:%M:%SZ",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": log_level,
                "formatter": "detailed",
                "stream": sys.stdout,
            },
            "error_console": {
                "class": "logging.StreamHandler",
                "level": "ERROR",
                "formatter": "detailed",
                "stream": sys.stderr,
            },
        },
        "loggers": {
            "app": {"level": log_level, "handlers": ["console"], "propagate": False},
            "uvicorn": {"level": "INFO", "handlers": ["console"], "propagate": False},
            "uvicorn.error": {
                "level": "INFO",
                "handlers": ["error_console"],
                "propagate": False,
            },
            "uvicorn.access": {
                "level": "INFO",
                "handlers": ["console"],
                "propagate": False,
            },
        },
        "root": {"level": log_level, "handlers": ["console"]},
    }

    # Use JSON formatting for production
    if os.getenv("ENVIRONMENT") == "production":
        handlers = config["handlers"]
        if isinstance(handlers, dict):
            console_handler = handlers.get("console")
            error_console_handler = handlers.get("error_console")
            if isinstance(console_handler, dict):
                console_handler["formatter"] = "json"
            if isinstance(error_console_handler, dict):
                error_console_handler["formatter"] = "json"

    return config


def setup_logging():
    """Setup logging configuration"""
    config = get_logging_config()
    logging.config.dictConfig(config)

    # Set specific loggers to WARNING to reduce noise
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
