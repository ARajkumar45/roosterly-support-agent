import httpx
import pytest
from pydantic import ValidationError

from agent.helpdesk import (
    AlreadyHandled,
    GaveUp,
    Helpdesk,
    Limiter,
    build_body,
    StopRun,
    TicketError,
)


def make_client(handler, clock, dry_run=False):
    """A Helpdesk that talks to a fake server (handler) on a fake clock."""
    limiter = Limiter(clock=clock.time, sleep=clock.sleep)
    return Helpdesk(
        url="https://fake.test/helpdesk",
        key="fake-key",
        email="tester@example.com",
        dry_run=dry_run,
        transport=httpx.MockTransport(handler),
        limiter=limiter,
        rng=lambda low, high: high,  # always the longest wait, so tests are fixed
    )


def answers(*responses):
    """A fake server that gives these answers in order, and counts the calls."""
    queue = list(responses)
    calls = []

    def handler(request):
        calls.append(request)
        return queue.pop(0)

    return handler, calls


def test_headers_on_every_request(clock):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    helpdesk = make_client(handler, clock)
    helpdesk.send("GET", "/status")
    helpdesk.send("GET", "/tickets", params={"limit": 10})
    helpdesk.send("POST", "/tickets/abc/handoff", json={"language": "nl"})

    assert len(seen) == 3
    for request in seen:
        assert request.headers["Authorization"] == "Bearer fake-key"
        assert request.headers["X-Candidate-Email"] == "tester@example.com"
    assert seen[2].headers["Content-Type"] == "application/json"


def test_20_calls_max_4_per_second(clock):
    starts = []

    def handler(request):
        starts.append(clock.time())
        return httpx.Response(200, json={})

    helpdesk = make_client(handler, clock)
    for _ in range(20):
        helpdesk.send("GET", "/status")

    assert len(starts) == 20
    for start in starts:
        in_window = [t for t in starts if start <= t < start + 1.0]
        assert len(in_window) <= 4


def test_503_then_success(clock):
    handler, calls = answers(httpx.Response(503), httpx.Response(200, json={"ok": 1}))
    response = make_client(handler, clock).request("GET", "/tickets")
    assert response.json() == {"ok": 1}
    assert len(calls) == 2
    assert 0.5 in clock.sleeps  # first backoff wait: 0.5 s at most


def test_503_gives_up_after_5(clock):
    handler, calls = answers(*[httpx.Response(503)] * 5)
    with pytest.raises(GaveUp):
        make_client(handler, clock).request("GET", "/tickets")
    assert len(calls) == 5
    # Waits double each time: 0.5, 1, 2, 4. No wait after the last try.
    assert [s for s in clock.sleeps if s >= 0.5] == [0.5, 1.0, 2.0, 4.0]


def test_timeout_is_retried(clock):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={})

    make_client(handler, clock).request("GET", "/tickets")
    assert len(calls) == 2


def test_429_waits_retry_after(clock):
    too_fast = httpx.Response(429, headers={"Retry-After": "7"})
    handler, calls = answers(too_fast, httpx.Response(200, json={}))
    make_client(handler, clock).request("GET", "/tickets")
    assert len(calls) == 2
    assert 7.25 in clock.sleeps  # 7 s from the header plus a small random extra


def test_429_no_header(clock):
    handler, calls = answers(httpx.Response(429), httpx.Response(200, json={}))
    make_client(handler, clock).request("GET", "/tickets")
    assert len(calls) == 2
    assert 1.25 in clock.sleeps  # fallback of 1 s plus a small random extra


def test_409_not_retried(clock):
    handler, calls = answers(httpx.Response(409, json={"error": "already_handled"}))
    with pytest.raises(AlreadyHandled):
        make_client(handler, clock).request("POST", "/tickets/abc/reply", json={})
    assert len(calls) == 1


def test_401_stops_run(clock):
    handler, calls = answers(httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(StopRun):
        make_client(handler, clock).request("GET", "/status")
    assert len(calls) == 1


def test_422_leaves_open(clock):
    handler, calls = answers(httpx.Response(422, json={"error": "validation_failed"}))
    with pytest.raises(TicketError):
        make_client(handler, clock).request("POST", "/tickets/abc/reply", json={})
    assert len(calls) == 1


# ---- reading tickets page by page ----

def ticket(ticket_id, handled=None):
    return {"id": ticket_id, "received_at": f"2026-01-01T00:00:{ticket_id[-2:]}",
            "subject": "s", "body": "b", "handled": handled}


def page(ids, next_cursor):
    return httpx.Response(200, json={"tickets": [ticket(i) for i in ids],
                                     "next_cursor": next_cursor})


def test_pagination_collects_all(clock):
    handler, calls = answers(
        page(["a03", "a01"], "a01"),
        httpx.Response(503),            # a failed page is asked for again
        page(["a02", "a01"], None),     # a01 comes twice, kept once
    )
    tickets = make_client(handler, clock).list_tickets()
    assert [t["id"] for t in tickets] == ["a01", "a02", "a03"]  # oldest first
    assert calls[1].url.params["cursor"] == "a01"
    assert calls[2].url.params["cursor"] == "a01"


def test_repeating_cursor_stops(clock):
    handler, calls = answers(page(["a01"], "x"), page(["a02"], "x"), page(["a03"], "x"))
    tickets = make_client(handler, clock).list_tickets()
    assert len(calls) == 2  # the same cursor twice means a loop, so stop
    assert len(tickets) == 2


# ---- posting a decision ----

HANDOFF = {"language": "nl", "category": "billing", "priority": "P2",
           "reason": "Refund request.", "handoff_note": "Customer wants a refund."}


def client_with_ticket(handler, clock, dry_run=False):
    helpdesk = make_client(handler, clock, dry_run)
    helpdesk.known_ids.add("a01")
    return helpdesk


def test_one_post_per_ticket(clock):
    handler, calls = answers(httpx.Response(200, json={}))
    outcome = client_with_ticket(handler, clock).post_action("a01", "handoff", HANDOFF)
    assert outcome == "posted"
    assert [c.method for c in calls] == ["POST"]


def test_timeout_after_save_no_second_post(clock):
    """The post is saved but the OK is lost. We must not post again."""
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=ticket("a01", handled={"action": "handoff"}))

    outcome = client_with_ticket(handler, clock).post_action("a01", "handoff", HANDOFF)
    assert outcome == "posted_confirmed_by_get"
    assert calls == ["POST", "GET"]


def test_503_checks_then_posts_again(clock):
    """503 means nothing was saved. The check agrees, so we post again."""
    handler, calls = answers(
        httpx.Response(503),
        httpx.Response(200, json=ticket("a01")),
        httpx.Response(200, json={}),
    )
    outcome = client_with_ticket(handler, clock).post_action("a01", "handoff", HANDOFF)
    assert outcome == "posted"
    assert [c.method for c in calls] == ["POST", "GET", "POST"]


def test_check_fails_no_second_post(clock):
    """If we cannot find out whether it was saved, we do not post again."""
    handler, calls = answers(httpx.Response(503), *[httpx.Response(503)] * 5)
    with pytest.raises(GaveUp):
        client_with_ticket(handler, clock).post_action("a01", "handoff", HANDOFF)
    assert [c.method for c in calls].count("POST") == 1


def test_409_is_counted_not_retried(clock):
    handler, calls = answers(httpx.Response(409))
    outcome = client_with_ticket(handler, clock).post_action("a01", "handoff", HANDOFF)
    assert outcome == "already_handled_409"
    assert len(calls) == 1


def test_dry_run_never_posts(clock):
    handler, calls = answers()
    with pytest.raises(RuntimeError):
        client_with_ticket(handler, clock, dry_run=True).post_action("a01", "handoff", HANDOFF)
    assert calls == []


def test_example_id_refused(clock):
    handler, calls = answers()
    with pytest.raises(ValueError):
        make_client(handler, clock).post_action("not-listed", "handoff", HANDOFF)
    assert calls == []


# ---- the body we send ----

BASE = {"language": "nl", "category": "question", "priority": "P4", "reason": "Why."}


def test_body_fields_per_action():
    reply = build_body("reply", {**BASE, "draft_reply": "Hoi", "confidence": 0.9})
    assert set(reply) == {"language", "category", "priority", "reason", "draft_reply", "confidence"}
    close = build_body("close", BASE)
    assert set(close) == {"language", "category", "priority", "reason"}
    with pytest.raises(ValidationError):  # a handoff needs a note
        build_body("handoff", BASE)
    with pytest.raises(ValidationError):  # a close takes no reply text
        build_body("close", {**BASE, "draft_reply": "Hoi"})


def test_other_language_handoff():
    body = build_body("handoff", {**BASE, "language": "other", "handoff_note": "French ticket."})
    assert body["language"] == "en"
    with pytest.raises(ValidationError):  # never reply in a language we cannot check
        build_body("reply", {**BASE, "language": "other", "draft_reply": "Bonjour"})


def test_empty_ticket_language():
    assert build_body("close", {**BASE, "language": None})["language"] == "nl"


def test_schema_rejects_unknown_category():
    with pytest.raises(ValidationError):
        build_body("close", {**BASE, "category": "complaint"})
