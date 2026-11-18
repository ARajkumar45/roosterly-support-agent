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


def finished_ids(log_dir):
    """Ticket ids that already have a final log line in this folder (not in sub-folders)."""
    ids = set()
    for file in Path(log_dir).glob("*.jsonl"):
        for line in file.read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:  # a line cut short by a crash
                continue
            if record.get("outcome") != "failed_left_open":
                ids.add(record.get("ticket_id"))
    return ids


def archive(log_dir, name):
    """Move the run logs into a sub-folder. Called after a reset, because the tickets
    are open again and the old lines no longer describe what is in the helpdesk."""
    files = list(Path(log_dir).glob("*.jsonl"))
    if files:
        target = Path(log_dir) / name
        target.mkdir(parents=True, exist_ok=True)
        for file in files:
            file.rename(target / file.name)
    return len(files)
