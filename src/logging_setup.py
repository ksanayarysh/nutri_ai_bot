# src/logging_setup.py
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def setup_logging() -> None:
    """
    Central logging setup.
    - Console logs (for cloud)
    - Optional rotating file logs (for local/dev)
    """

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Prevent duplicated handlers (important for reloads)
    if root.handlers:
        return

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # -------------------------
    # Console (always)
    # -------------------------
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # -------------------------
    # Optional file logging (local)
    # -------------------------
    log_file = os.getenv("LOG_FILE")
    if log_file:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Quiet noisy libs a bit
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
