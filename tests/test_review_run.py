"""Tests for the second review: the runner never uses the real repo's state files in tests."""

import pytest

from agent.run import run
from tests.fakes import FakeDesk, make_settings


def test_run_needs_explicit_state_paths(clock):
    """run() has no default paths, so a test can never read the real lock file by accident."""
    desk = FakeDesk([])
    with pytest.raises(TypeError):
        run(make_settings(), desk.client(clock), False, assume_yes=True, say=lambda line: None)


def test_log_line_has_the_model_confidence_for_a_handoff(monkeypatch, clock, tmp_path):
    from agent import llm
    from tests.fakes import covered_form, fake_model
    from tests.test_run import log_lines, start, tickets

    fake_model(monkeypatch, lambda user: covered_form(llm.load_faq(), confidence=0.42))
    start(FakeDesk(tickets("Ik wil mijn geld terug.")), clock, tmp_path)
    line = log_lines(tmp_path)[0]
    assert line["action"] == "handoff" and line["model_confidence"] == 0.42
    assert "confidence" not in line["sent"]  # never sent for a handoff


def test_a_long_retry_after_is_respected():
    """A 429 with Retry-After: 60 waits the full 60 s. Retrying sooner earns another 429."""
    import httpx

    from agent.helpdesk import Helpdesk

    desk = Helpdesk("https://fake.test", "k", "a@b.c", dry_run=True, rng=lambda low, high: 0)
    assert desk.retry_after(httpx.Response(429, headers={"Retry-After": "60"})) == 60
    assert desk.retry_after(httpx.Response(429, headers={"Retry-After": "99999"})) == 300
