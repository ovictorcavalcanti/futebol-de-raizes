"""Logs estruturados (fase 11): uma linha JSON por registro, com id da requisição."""

import contextvars
import json
import logging
import logging.config
from datetime import datetime, timezone

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
user_var: contextvars.ContextVar[str] = contextvars.ContextVar("user", default="")

# color_message: cópia de msg com cores ANSI que o uvicorn manda em `extra`.
_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "color_message"}


class RequestContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(record, "request_id", None) or request_id_var.get()
        record.user = getattr(record, "user", None) or user_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key in _RESERVED or key.startswith("_") or value in (None, ""):
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"fdr.{name}")


def configure(config: dict) -> None:
    """LOGGING_CONFIG do settings: aplica LOGGING e manda os avisos do Python
    (`warnings`) para o logger "py.warnings", no mesmo formato dos demais registros.

    Sem isso, um aviso (ex.: o do Django ao servir um estático do WhiteNoise pelo
    ASGI) sairia em texto puro no meio das linhas JSON.
    """
    logging.config.dictConfig(config)
    logging.captureWarnings(True)
