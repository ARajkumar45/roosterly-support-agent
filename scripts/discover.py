"""Read-only look at the helpdesk API. Makes GET requests only.

Prints the shape of each answer (field names and types, no ticket text)
and writes the same to docs/api_shapes.md.

Run from the repo root:  python scripts/discover.py
"""

import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PAUSE = 0.30  # seconds between requests, well under 5 a second


def shape(value):
    """Describe a JSON value by its field names and types only."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        inner = shape(value[0]) if value else "empty"
        return [f"list of {len(value)}", inner]
    if value is None:
        return "null"
    return type(value).__name__


def mask(email):
    name, _, domain = email.partition("@")
    return name[:2] + "***@" + domain


def get(client, path, headers, params=None):
    """GET with a few retries on 503. Never prints headers."""
    for attempt in range(5):
        time.sleep(PAUSE)
        response = client.get(path, headers=headers, params=params)
        if response.status_code == 503:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.status_code != 200:
            sys.exit(f"GET {path} gave {response.status_code}. Stopping.")
        return response.json()
    sys.exit(f"GET {path} gave 503 five times. Stopping.")


def find_tickets(answer):
    """The ticket list may be bare or wrapped in an object. Return the list."""
    if isinstance(answer, list):
        return answer
    for item in answer.values():
        if isinstance(item, list):
            return item
    return []


def main():
    load_dotenv(ROOT / ".env")
    url = os.environ.get("HELPDESK_URL", "").strip().rstrip("/")
    key = os.environ.get("HELPDESK_KEY", "").strip()
    email = os.environ.get("CANDIDATE_EMAIL", "").strip()
    name = os.environ.get("CANDIDATE_NAME", "").strip()
    if not url or not key or email.count("@") != 1:
        sys.exit("Fill HELPDESK_URL, HELPDESK_KEY and CANDIDATE_EMAIL in .env first.")

    headers = {"Authorization": f"Bearer {key}", "X-Candidate-Email": email}
    print(f"Account: {mask(email)}")
    lines = ["# API shapes", "", "Field names and types only. No ticket text.", ""]

    def record(title, described, extra=None):
        print(f"\n{title}\n{described}")
        lines.extend([f"## {title}", "", "```", str(described), "```", ""])
        if extra:
            print(extra)
            lines.extend([extra, ""])

    with httpx.Client(base_url=url, timeout=httpx.Timeout(30, connect=5)) as client:
        # The name header is optional and sent once, here.
        status = get(client, "/status", {**headers, "X-Candidate-Name": name})
        safe = {k: v for k, v in status.items() if k == "mode" or isinstance(v, (int, bool))}
        record("GET /status", shape(status), f"Values (mode and counts only): {safe}")

        examples = get(client, "/examples", headers)
        record("GET /examples", shape(examples), f"Count: {len(find_tickets(examples))}")

        page = get(client, "/tickets", headers, {"limit": 3, "status": "all"})
        tickets = find_tickets(page)
        channels = sorted({str(t.get("channel")) for t in tickets})
        plans = sorted({str(t.get("customer_plan")) for t in tickets})
        record(
            "GET /tickets?limit=3&status=all",
            shape(page),
            f"In this page: channel values {channels}, customer_plan values {plans}",
        )

        if tickets:
            one = get(client, f"/tickets/{tickets[0]['id']}", headers)
            record("GET /tickets/{id}", shape(one))

    (ROOT / "docs" / "api_shapes.md").write_text("\n".join(lines))
    print("\nWrote docs/api_shapes.md")


if __name__ == "__main__":
    main()
