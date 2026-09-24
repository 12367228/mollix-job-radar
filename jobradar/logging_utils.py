"""Structured events and redacted network diagnostics; never dump response bodies."""

from datetime import datetime, timezone
import json
import logging
import re
import unicodedata
from urllib.parse import unquote

LOGGER = logging.getLogger("jobradar")


def configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    LOGGER.setLevel(logging.INFO)
    # httpx logs request URLs, including Telegram's token in its URL path.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def event(name: str, **fields: object) -> None:
    LOGGER.info(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "event": name, **fields}, ensure_ascii=False))


def sanitized_network_message(message: str, *, secrets: tuple[str, ...] = ()) -> str:
    # Decode percent escapes before redaction, including double-encoded URLs/tokens.
    for _ in range(2):
        message = unquote(message)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"https?://[^\s<>\"']+", "[REDACTED_URL]", message, flags=re.I)
    message = re.sub(r"\d{5,20}:[A-Za-z0-9_-]{20,100}", "[REDACTED_TOKEN]", message)
    message = "".join(" " if unicodedata.category(c).startswith("C") else c for c in message)
    return " ".join(message.split())[:500] or "No additional network details"


def telegram_network_error(endpoint: str, error: Exception, *, token: str, chat_id: str = "") -> None:
    # Log only this safe record, never error.request, exc_info or a raw traceback.
    event("telegram_network_error", telegram_endpoint=endpoint, exception_type=type(error).__name__,
          message=sanitized_network_message(str(error), secrets=(token, chat_id)))
