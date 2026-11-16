import httpx
import pytest

from agent.helpdesk import (
    AlreadyHandled,
    GaveUp,
    Helpdesk,
    Limiter,
    StopRun,
    TicketError,
)


def make_client(handler, clock):
    """A Helpdesk that talks to a fake server (handler) on a fake clock."""
    limiter = Limiter(clock=clock.time, sleep=clock.sleep)
    return Helpdesk(
        url="https://fake.test/helpdesk",
        key="fake-key",
        email="tester@example.com",
        dry_run=False,
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
