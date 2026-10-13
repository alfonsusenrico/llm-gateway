"""Process logging: JSON lines in production, readable lines in development.

Log records carry identifiers, counts, durations, and states only. Prompts,
model output, keys, and request bodies are never logged.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

from llm_gateway.settings import Settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
# "<project_id>/<environment>" once a request is authenticated.
project_context_var: ContextVar[str] = ContextVar("project_context", default="")
# HTTP client libraries log full request URLs at INFO and DEBUG, and some provider
# URLs carry credentials (the Telegram bot token is part of the path).
_URL_LOGGING_LIBRARIES = ("httpx2", "httpcore2")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.project = project_context_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", "")
        if request_id:
            entry["request_id"] = request_id
        project = getattr(record, "project", "")
        if project:
            entry["project"] = project
        if record.exc_info and record.exc_info[0] is not None:
            # Only the exception class: messages can echo provider text or values.
            entry["exception"] = record.exc_info[0].__name__
        return json.dumps(entry, separators=(",", ":"))


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    if settings.app_env == "production":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    limit_library_logging()


def limit_library_logging() -> None:
    for name in _URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
