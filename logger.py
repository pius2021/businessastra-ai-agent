import logging
import logging.config
import os

def setup_logging():
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_file = os.path.join(log_dir, "app.log")

    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            },
            "detailed": {
                "format": "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s"
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": "INFO",
                "stream": "ext://sys.stdout"
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "detailed",
                "level": "INFO",
                "filename": log_file,
                "maxBytes": 5 * 1024 * 1024,  # 5 MB
                "backupCount": 5,
                "encoding": "utf8"
            }
        },
        "root": {
            "level": "INFO",
            "handlers": ["console", "file"]
        },
        "loggers": {
            "uvicorn": {
                "level": "INFO",
                "handlers": ["console", "file"],
                "propagate": False
            },
            "uvicorn.error": {
                "level": "INFO",
                "handlers": ["console", "file"],
                "propagate": False
            },
            "uvicorn.access": {
                "level": "INFO",
                "handlers": ["console", "file"],
                "propagate": False
            },
            "fastapi": {
                "level": "INFO",
                "handlers": ["console", "file"],
                "propagate": False
            }
        }
    }

    logging.config.dictConfig(log_config)
