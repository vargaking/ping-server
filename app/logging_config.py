"""Application logging configuration.

Configures a rotating file handler and attaches it to the root logger and uvicorn
loggers so application and server logs are persisted to a file.
"""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_FILE = os.getenv("LOG_FILE", os.path.join(LOG_DIR, "app.log"))
MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024))
BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", 5))

# Ensure log directory exists
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

formatter = logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s")

handler = RotatingFileHandler(
    LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT)
handler.setFormatter(formatter)
handler.setLevel(LOG_LEVEL)

root = logging.getLogger()
root.setLevel(LOG_LEVEL)

# Avoid adding duplicate file handlers when the module is reloaded
has_same = False
for h in root.handlers:
    if isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == os.path.abspath(LOG_FILE):
        has_same = True
        break
if not has_same:
    root.addHandler(handler)

# Attach same handler/level to uvicorn loggers so server logs go to file too
for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logger = logging.getLogger(name)
    logger.setLevel(LOG_LEVEL)
    if not any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == os.path.abspath(LOG_FILE) for h in logger.handlers):
        logger.addHandler(handler)

# Export a convenience logger for the application code
logger = logging.getLogger("app")
