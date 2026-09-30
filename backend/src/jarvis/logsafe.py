"""Logging hygiene: HTTP client libraries log full request URLs at INFO, and some URLs are secrets
(private ICS feeds carry an access token). Keep them quiet and redact token-looking URL parts everywhere."""
from __future__ import annotations

import logging
import re

_SECRETISH = re.compile(r"(https?://[^\s\"']*?)(/feeds/[^\s\"']+|[?&](?:token|key|access_token|sig|signature)=[^\s\"'&]+)", re.I)


class RedactURLs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        red = _SECRETISH.sub(lambda m: m.group(1) + "/[redacted]", msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True


def configure() -> None:
    for name in ("httpx", "httpcore", "hpack", "urllib3", "huggingface_hub"):
        logging.getLogger(name).setLevel(logging.WARNING)
    f = RedactURLs()
    root = logging.getLogger()
    root.addFilter(f)
    for h in root.handlers:
        h.addFilter(f)
