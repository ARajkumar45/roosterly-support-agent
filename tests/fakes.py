"""A fake helpdesk and a fake model for tests. Nothing here touches the network."""

import json
import re

import httpx

from agent import llm
from agent.config import Settings
from agent.helpdesk import Helpdesk, Limiter

URL = "https://fake.test/helpdesk"


class FakeDesk:
    """Behaves like the helpdesk API: lists tickets, saves one decision each, 409 on a second."""

    def __init__(self, tickets, mode="practice"):
        self.tickets = {t["id"]: {**t, "handled": None} for t in tickets}
        self.mode = mode
        self.posts = []  # every decision that was saved: (ticket id, action, body)
        self.post_hook = None  # tests set this to inject a failure into a POST

    def handler(self, request):
        path = request.url.path.removeprefix("/helpdesk")
        if path == "/status":
            handled = sum(t["handled"] is not None for t in self.tickets.values())
            return httpx.Response(200, json={
                "mode": self.mode, "open_tickets": len(self.tickets) - handled,
                "handled_tickets": handled})
        if path == "/tickets":
            only_open = request.url.params.get("status", "open") == "open"
            found = [t for t in self.tickets.values() if not (only_open and t["handled"])]
            return httpx.Response(200, json={"tickets": found, "next_cursor": None})
        if path == "/practice/reset" and request.method == "POST":
            for ticket in self.tickets.values():
                ticket["handled"] = None
            return httpx.Response(200, json={"ok": True})
        match = re.fullmatch(r"/tickets/([^/]+)(?:/(reply|handoff|close))?", path)
        if not match or match.group(1) not in self.tickets:
            return httpx.Response(404, json={"error": "not_found"})
        ticket_id, action = match.groups()
        if action is None:
            return httpx.Response(200, json=self.tickets[ticket_id])

        def save():
            if self.tickets[ticket_id]["handled"]:
                return httpx.Response(409, json={"error": "already_handled"})
            body = json.loads(request.content)
            self.tickets[ticket_id]["handled"] = {"action": action}
            self.posts.append((ticket_id, action, body))
            return httpx.Response(200, json={"ok": True})

        if self.post_hook:
            return self.post_hook(request, ticket_id, save)
        return save()

    def client(self, clock, dry_run=False):
        return Helpdesk(
            URL, "fake-key", "tester@example.com", dry_run,
            transport=httpx.MockTransport(self.handler),
            limiter=Limiter(clock=clock.time, sleep=clock.sleep),
            rng=lambda low, high: high,
        )


def make_settings(provider="fake", **changes):
    base = dict(
        helpdesk_url=URL, helpdesk_key="fake-key", email="tester@example.com", name="",
        provider=provider, model="fake-model", view_model="fake-model", llm_key="",
        threshold=0.75, max_tokens=2000, spend_cap_eur=1.0,
    )
    return Settings(**{**base, **changes})


def covered_form(faq, **changes):
    """A valid form for a how-to that the first help-centre entry covers.

    The quote is cut from the real FAQ at test time, so no FAQ text lives in the tests.
    """
    faq_id, entry = next(iter(faq.items()))
    base = dict(
        language="nl", category="question", priority="P4", action="reply",
        must_handoff_topic="none", many_users_blocked=False, asks_plan_change=False,
        already_tried=False, summary="Asks a how-to question.",
        sub_questions=[{"text": "How does this work?", "covered": True, "faq_id": faq_id}],
        faq_quotes=[{"faq_id": faq_id, "quote": " ".join(entry.split()[4:9])}],
        draft_reply="Hoi, dat kan in de app. Dat is het antwoord op je vraag. Groet, Team Roosterly",
        handoff_note="Customer asks a how-to question.",
        reason="How-to question answered by the help centre. No urgency.", confidence=0.9,
    )
    return {**base, **changes}


def english_like(draft):
    """A stand-in English translation: plain English with the draft's digits and links, so the
    number and link compare matches, as a faithful translation would."""
    from agent.decide import urls
    keep = re.findall(r"\d+(?:[.,]\d+)?", urls_removed(draft)) + sorted(urls(draft))
    return f"Hello, here is the answer for you: {' '.join(keep)}. Kind regards, team roosterly"


def urls_removed(text):
    from agent.decide import URL
    return URL.sub(" ", text)


def fake_model(monkeypatch, answer_for, spam_answer="CUSTOMER"):
    """Install a fake provider. answer_for(user_message) returns a form dict, a string or an Answer.

    The English back-translation call returns plain English with the draft's numbers and links.
    The second spam check answers spam_answer.
    """
    calls = []

    def provider(settings, model, system, user):
        calls.append(user)
        if system == llm.VIEW_PROMPT:
            return llm.Answer(english_like(user), 10, 10)
        if system == llm.SPAM_PROMPT:
            calls.append("second spam check")
            return llm.Answer(spam_answer, 10, 1)
        answer = answer_for(user)
        if isinstance(answer, llm.Answer):
            return answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return llm.Answer(text, 100, 50)

    monkeypatch.setitem(llm.PROVIDERS, "fake", provider)
    return calls
