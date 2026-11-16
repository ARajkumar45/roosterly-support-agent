"""Test setup. Tests never touch the real helpdesk and never see a real key."""

import pytest


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    monkeypatch.setenv("HELPDESK_URL", "https://fake.test/helpdesk")
    monkeypatch.setenv("CANDIDATE_EMAIL", "tester@example.com")
    monkeypatch.delenv("HELPDESK_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)


class FakeClock:
    """A clock that only moves when someone sleeps. Makes timing tests instant."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []  # every sleep, so tests can check the waits

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()
