"""The only place that writes log lines. Everything is redacted on the way out."""

import json
from pathlib import Path

from agent.rules import redact


def clean(value):
    """Redact every string inside a nested value."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


def write_line(path, record):
    """Append one JSON line. Called once per ticket, when it reaches its final state."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        file.write(json.dumps(clean(record), ensure_ascii=False) + "\n")
