"""Reliability test over real HTTP, against a local helpdesk that misbehaves on purpose.

  python -m scripts.reliability_test

It starts a small helpdesk on this computer (never Nordavo's), runs the real agent
code against it in separate processes, and counts exactly what was saved. The model
is a fake, so this costs nothing. Each scenario prints PASS or FAIL with the counts.

The local helpdesk follows api_reference.md: same paths, same headers, same errors.
It is made worse than the real one: more 503s, replies that get lost after the
decision was saved, a stricter rate limit, a process that is killed halfway.
"""

import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KEY, EMAIL = "local-test-key", "tester@example.com"
TICKETS = 30


class LocalDesk:
    """The state of the local helpdesk, and counters for everything it saw."""

    def __init__(self, p503=0.0, p_lost=0.0, per_second=5, seed=1):
        self.p503, self.p_lost, self.per_second = p503, p_lost, per_second
        self.random = random.Random(seed)
        self.lock = threading.Lock()
        self.handled = {}  # ticket id -> the one decision that was saved
        self.recent = []  # times of the latest requests, for the rate limit
        self.blocked_until = 0.0  # after a 429 the client must wait until this time
        self.count = dict(requests=0, saved=0, s503=0, s429=0, s409=0, lost=0, too_early=0)
        self.tickets = [
            {"id": f"L{n:03}", "received_at": f"2026-01-01T00:{n:02}:00+00:00", "channel": "email",
             "customer_plan": "Basis", "subject": "Vraag",
             # every third ticket asks for money back, so the run has replies and handoffs
             "body": "Ik wil mijn geld terug." if n % 3 == 0 else "Hoe voeg ik iemand toe aan het rooster?"}
            for n in range(1, TICKETS + 1)
        ]

    def view(self, ticket):
        return {**ticket, "handled": self.handled.get(ticket["id"])}

    def answer(self, method, path, query, headers, body):
        """Returns (status, json body, extra headers), or 'drop' to cut the connection."""
        with self.lock:
            now = time.monotonic()
            self.count["requests"] += 1
            if headers.get("Authorization") != f"Bearer {KEY}":
                return 401, {"error": "unauthorized"}, {}
            if not headers.get("X-Candidate-Email"):
                return 400, {"error": "missing_email"}, {}
            if now < self.blocked_until:
                self.count["too_early"] += 1  # the client did not wait for Retry-After
            self.recent = [t for t in self.recent if now - t < 1.0]
            if len(self.recent) >= self.per_second:
                self.count["s429"] += 1
                self.blocked_until = now + 1.0
                return 429, {"error": "rate_limited"}, {"Retry-After": "1"}
            self.recent.append(now)

            if path == "/status":
                return 200, {"mode": "practice", "open_tickets": TICKETS - len(self.handled),
                             "handled_tickets": len(self.handled)}, {}
            if self.random.random() < self.p503:
                self.count["s503"] += 1
                return 503, {"error": "service_unavailable"}, {}

            if path == "/tickets":
                only_open = query.get("status", ["open"])[0] == "open"
                rows = [t for t in self.tickets if not (only_open and t["id"] in self.handled)]
                cursor = query.get("cursor", [None])[0]
                if cursor:
                    rows = [t for t in rows if t["id"] > cursor]
                page = rows[:10]
                more = page[-1]["id"] if len(rows) > 10 else None
                return 200, {"tickets": [self.view(t) for t in page], "next_cursor": more}, {}

            parts = path.strip("/").split("/")
            ticket = next((t for t in self.tickets if len(parts) > 1 and t["id"] == parts[1]), None)
            if parts[0] != "tickets" or ticket is None:
                return 404, {"error": "not_found"}, {}
            if len(parts) == 2 and method == "GET":
                return 200, self.view(ticket), {}
            if len(parts) == 3 and method == "POST" and parts[2] in ("reply", "handoff", "close"):
                if ticket["id"] in self.handled:
                    self.count["s409"] += 1
                    return 409, {"error": "already_handled"}, {}
                self.handled[ticket["id"]] = {"action": parts[2], "body": body}
                self.count["saved"] += 1
                if self.random.random() < self.p_lost:
                    self.count["lost"] += 1
                    return "drop"  # saved, but the client never hears it
                return 200, {"ok": True}, {}
            return 404, {"error": "not_found"}, {}


def serve(desk):
    class Handler(BaseHTTPRequestHandler):
        def handle_any(self):
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"null") if length else None
            result = desk.answer(self.command, url.path, parse_qs(url.query), self.headers, body)
            if result == "drop":
                self.connection.close()
                return
            status, payload, extra = result
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = handle_any

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


# ---- the agent, started as its own process ----


def drive(url, work, key):
    """Runs inside the child process: the real runner and client, with a fake model."""
    from agent import llm
    from agent.helpdesk import Helpdesk
    from agent.run import run
    from tests.fakes import covered_form, make_settings

    faq = llm.load_faq()

    def fake(settings, model, system, user):
        text = user if system == llm.VIEW_PROMPT else json.dumps(covered_form(faq))
        return llm.Answer(text, 100, 50)

    llm.PROVIDERS["fake"] = fake
    work = Path(work)
    settings = make_settings(helpdesk_url=url, helpdesk_key=key, email=EMAIL)
    helpdesk = Helpdesk(url, key, EMAIL, dry_run=False)
    return run(settings, helpdesk, False, assume_yes=True, log_dir=work / "logs",
               marker=work / ".agent_account", final_marker=work / ".final_run_done")


def start_agent(url, work, key=KEY):
    return subprocess.Popen(
        [sys.executable, "-m", "scripts.reliability_test", "--drive", url, str(work), key],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=Path(__file__).resolve().parent.parent,
    )


def posted_ids(work):
    """Ticket ids that the agent's own log says were posted, in all runs together."""
    ids = []
    for file in sorted(Path(work, "logs").glob("*.jsonl")):
        for line in file.read_text().splitlines():
            record = json.loads(line)
            if str(record.get("outcome", "")).startswith("posted"):
                ids.append(record["ticket_id"])
    return ids


# ---- scenarios ----


def scenario(name, checks, desk, extra=""):
    ok = all(passed for _, passed in checks)
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    for text, passed in checks:
        print(f"      {'ok ' if passed else 'NO '} {text}")
    print(f"      helpdesk saw: {desk.count} {extra}\n")
    return ok


def all_once(desk):
    return len(desk.handled) == TICKETS and desk.count["saved"] == TICKETS


def bad_day():
    desk = LocalDesk(p503=0.25, p_lost=0.15, per_second=5)
    server, url = serve(desk)
    with tempfile.TemporaryDirectory() as work:
        code = start_agent(url, work).wait()
        if code != 0:  # a ticket that used all 5 tries is left open; a re-run must finish it
            code = start_agent(url, work).wait()
        logged = posted_ids(work)
    server.shutdown()
    return scenario("Bad day: 1 in 4 calls fails, 1 in 7 saved decisions gets no reply", [
        (f"all {TICKETS} tickets saved exactly once", all_once(desk)),
        ("the agent never sent a second decision (409 count is 0)", desk.count["s409"] == 0),
        ("the agent never went over the rate limit (429 count is 0)", desk.count["s429"] == 0),
        (f"the agent's log has each ticket once ({len(logged)} lines, {len(set(logged))} ids)",
         sorted(logged) == sorted(desk.handled)),
        ("failures really happened", desk.count["s503"] > 0 and desk.count["lost"] > 0),
        ("exit code 0", code == 0),
    ], desk)


def hard_crash():
    desk = LocalDesk(p503=0.05)
    server, url = serve(desk)
    with tempfile.TemporaryDirectory() as work:
        first = start_agent(url, work)
        while desk.count["saved"] < 12 and first.poll() is None:
            time.sleep(0.01)
        first.send_signal(signal.SIGKILL)  # no clean-up at all, like a power cut
        first.wait()
        saved_before = desk.count["saved"]
        code = start_agent(url, work).wait()
        logged = posted_ids(work)
    server.shutdown()
    return scenario("Hard crash: the process is killed halfway, then started again", [
        ("the first run was killed halfway", 0 < saved_before < TICKETS),
        (f"all {TICKETS} tickets saved exactly once", all_once(desk)),
        ("the second run sent no decision for a handled ticket (409 count is 0)", desk.count["s409"] == 0),
        (f"the log has all {TICKETS} tickets, each once, also the one saved just before the kill "
         f"({len(logged)} lines)", sorted(logged) == sorted(desk.handled)),
        ("second run exit code 0", code == 0),
    ], desk, f"| saved before the kill: {saved_before}, log lines: {len(logged)}")


def strict_rate_limit():
    desk = LocalDesk(per_second=2)  # stricter than the 5 a second we plan for
    server, url = serve(desk)
    with tempfile.TemporaryDirectory() as work:
        code = start_agent(url, work).wait()
    server.shutdown()
    return scenario("Rate limit stricter than expected: 2 calls a second", [
        ("429s really happened", desk.count["s429"] > 0),
        ("after every 429 the agent waited the full Retry-After", desk.count["too_early"] == 0),
        (f"all {TICKETS} tickets saved exactly once", all_once(desk)),
        ("409 count is 0", desk.count["s409"] == 0),
        ("exit code 0", code == 0),
    ], desk)


def two_agents():
    desk = LocalDesk(per_second=50)
    server, url = serve(desk)
    with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
        agents = [start_agent(url, one), start_agent(url, two)]
        codes = [agent.wait() for agent in agents]
    server.shutdown()
    return scenario("Two agents started at the same time by mistake", [
        (f"all {TICKETS} tickets saved exactly once", all_once(desk)),
        ("the helpdesk refused every second decision with a 409, and no agent retried one",
         desk.count["s409"] <= TICKETS),
        ("both agents finished without a crash", all(code in (0, 1) for code in codes)),
    ], desk, f"| exit codes {codes}")


def helpdesk_down():
    desk = LocalDesk(p503=1.0)
    server, url = serve(desk)
    with tempfile.TemporaryDirectory() as work:
        code = start_agent(url, work).wait()
    server.shutdown()
    return scenario("Helpdesk completely down", [
        ("nothing was saved", desk.count["saved"] == 0),
        ("the agent stopped with exit code 1 and did not crash", code == 1),
        ("it stopped after 5 tries, not forever", desk.count["s503"] == 5),
    ], desk)


def wrong_key():
    desk = LocalDesk()
    server, url = serve(desk)
    with tempfile.TemporaryDirectory() as work:
        code = start_agent(url, work, key="wrong-key").wait()
    server.shutdown()
    return scenario("Wrong key", [
        ("nothing was saved", desk.count["saved"] == 0),
        ("the agent stopped at the first 401 with exit code 2", code == 2 and desk.count["requests"] == 1),
    ], desk)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--drive":
        sys.exit(drive(*sys.argv[2:5]))
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    print(f"Reliability test: {TICKETS} tickets per scenario, real HTTP on this computer, fake model.\n")
    results = [test() for test in (bad_day, hard_crash, strict_rate_limit, two_agents,
                                   helpdesk_down, wrong_key)]
    print(f"{sum(results)} of {len(results)} scenarios passed.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
