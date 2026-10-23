"""Operational logs, as one JSON object per line for logs that leave the machine (the container's,
which the AWS host sends to CloudWatch), or as words on a terminal.

A line is an event with fields the code chose, or a library's own words. It never holds a CV
line, a job description, a prompt, a model answer, a key or a page token: an error is logged by
its type and the place it was raised, never its message or traceback, and a library's line keeps
its words only when they are known fixed wording (the server's own), never the values filled in,
since any other text can quote what the user wrote.
"""

import contextvars
import json
import logging
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

# Where no one has set up logging (as in the tests), nothing is printed; the app sets it up.
logging.getLogger("workbench").addHandler(logging.NullHandler())

# The request the running code serves; the web app sets it for each request.
request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


def elapsed_ms(started: float) -> int:
    """Milliseconds since ``started``, a time.monotonic() reading."""
    return round((time.monotonic() - started) * 1000)


def event(logger: logging.Logger, name: str, *, level: int = logging.INFO, error: bool = False, **fields: Any) -> None:
    """Log the event ``name`` with ``fields`` (values the code chose, never text from the user)
    and the id of the request it happens in. With ``error``, the exception being handled goes
    along: on a terminal in full, in JSON by type and place only."""
    current = request_id.get()
    if current and "request_id" not in fields:
        fields = {"request_id": current, **fields}
    words = " ".join([name, *(f"{key}={value}" for key, value in fields.items())])
    logger.log(level, words, exc_info=error, extra={"event": name, "fields": fields})


WITHHELD = "(left out: a library's message, which may hold text from a CV)"
# Library messages known to be fixed wording (Uvicorn's, as templates); only these are kept. A
# template alone proves nothing: "parse failed for " + text + ": %s" is one too.
FIXED_WORDING = frozenset({
    "Started server process [%d]",
    "Finished server process [%d]",
    "Uvicorn running on %s://%s:%d (Press CTRL+C to quit)",
    "Waiting for application startup.",
    "Application startup complete.",
    "Application startup failed. Exiting.",
    "Waiting for application shutdown.",
    "Application shutdown complete.",
    "Application shutdown failed. Exiting.",
    "Shutting down",
    "Waiting for connections to close. (CTRL+C to force quit)",
    "Waiting for background tasks to complete. (CTRL+C to force quit)",
    "Received SIGINT, exiting.",
    "Received SIGTERM, exiting.",
    "Invalid HTTP request received.",
    "Unsupported upgrade request.",
    "Exception in ASGI application",
    "ASGI callable returned without starting response.",
    "ASGI callable returned without completing response.",
})


class JsonLines(logging.Formatter):
    """One JSON object per line: the time, level and logger; the event and its fields, or a
    library's words if they are known fixed wording (otherwise nothing: they may have been put
    together from anything); the request it was logged in; and for an error only its type and the
    file and line it came from."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        if hasattr(record, "event"):
            line["event"] = record.event
            line.update(record.fields)
        else:
            known = isinstance(record.msg, str) and record.msg.strip() in FIXED_WORDING
            line["message"] = record.msg.strip() if known else WITHHELD
            current = request_id.get()
            if current:
                line["request_id"] = current
        if record.exc_info and record.exc_info[0] is not None:
            kind, _, trace = record.exc_info
            line["error"] = kind.__name__
            frames = traceback.extract_tb(trace)
            if frames:
                line["at"] = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}"
        return json.dumps(line, ensure_ascii=False, default=str)


def configure(stream: TextIO) -> None:
    """Write every log line to ``stream`` as JSON: the workbench's events and the server's own
    lines from INFO up, other libraries' only when they warn."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLines())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    for name in ("workbench", "uvicorn.error"):
        logging.getLogger(name).setLevel(logging.INFO)
