"""Tests for the eval fix and for the hardening done with it. Each rule is general
and is shown on a ticket written for the test."""

import pytest

from agent import llm
from agent.rules import guard
from agent.run import triage
from tests.fakes import covered_form, fake_model, make_settings

FAQ = llm.load_faq()
ADVERT = "Goedkope zonnepanelen voor uw bedrijf. Bel vandaag nog voor een gratis offerte."
CLOSE = dict(action="close", category="other", draft_reply=None)


def ticket(body, subject="Aanbieding"):
    return {"id": "x1", "subject": subject, "body": body, "customer_plan": "Basis", "channel": "email"}


def decide_with(monkeypatch, body, form, spam_answer):
    calls = fake_model(monkeypatch, lambda user: covered_form(FAQ, **form), spam_answer)
    decision, found, *_ = triage(ticket(body), make_settings(), FAQ)
    return decision, found, calls


# ---- close needs two signals; the second may be a separate, narrow model call ----


def test_spam_without_a_list_word_closes_when_both_calls_agree(monkeypatch):
    assert guard("Aanbieding", ADVERT).close_signal is None  # no word from the spam list
    decision, found, calls = decide_with(monkeypatch, ADVERT, CLOSE, "SPAM")
    assert decision.action == "close" and "second check" in found.close_signal
    assert "second spam check" in calls


@pytest.mark.parametrize("second_answer", ["CUSTOMER", "Probably spam, but I am not sure.", ""])
def test_no_close_unless_the_second_call_says_exactly_spam(monkeypatch, second_answer):
    decision, _, _ = decide_with(monkeypatch, ADVERT, CLOSE, second_answer)
    assert decision.action == "handoff" and decision.handoff_reason == "close_disagree"


def test_any_guard_hint_blocks_the_second_check(monkeypatch):
    body = ADVERT + " Anders gaan we stoppen."  # a weak hint that a customer may be leaving
    assert guard("Aanbieding", body).tier2
    decision, _, calls = decide_with(monkeypatch, body, CLOSE, "SPAM")
    assert decision.action == "handoff" and "second spam check" not in calls


def test_second_check_is_not_asked_for_a_normal_ticket(monkeypatch):
    decision, _, calls = decide_with(monkeypatch, "Hoe voeg ik iemand toe aan het rooster?", {}, "SPAM")
    assert decision.action == "reply" and "second spam check" not in calls


# ---- must-handoff wordings that the first list missed ----


@pytest.mark.parametrize("body, topic", [
    ("Ik wil opzeggen per volgende maand.", "cancel"),
    ("We want to cancel as soon as possible.", "cancel"),
    ("Ich möchte zum Monatsende kündigen.", "cancel"),
    ("Er is deze maand dubbel betaald.", "refund"),
    ("We were charged twice in October.", "refund"),
    ("Please delete my personal data.", "gdpr"),
    ("Ik doe een beroep op mijn recht op vergetelheid.", "gdpr"),
    ("We gaan overstappen als dit niet beter wordt.", "churn"),
])
def test_common_wordings_force_a_handoff(body, topic):
    assert topic in guard("Vraag", body).tier1


def test_it_does_not_work_alone_is_not_already_tried(monkeypatch):
    """A problem report is not the same as 'I tried your steps'. The model's own field decides."""
    body = "De app werkt niet op mijn telefoon. Wat kan ik doen?"
    decision, _, _ = decide_with(monkeypatch, body, {}, "CUSTOMER")
    assert decision.action == "reply"
    tried, _, _ = decide_with(monkeypatch, body, dict(already_tried=True), "CUSTOMER")
    assert tried.handoff_reason == "already_tried"


# ---- the model call cannot be redirected by the shell ----


def test_model_address_is_fixed(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, **options):
            seen.update(options)
            raise RuntimeError("stop here")

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://somewhere-else.example")
    monkeypatch.setattr(llm.anthropic, "Anthropic", Client)
    with pytest.raises(RuntimeError):
        llm.claude(make_settings(llm_key="k"), "m", "s", "u")
    assert seen["base_url"] == "https://api.anthropic.com"


def test_prompt_states_the_coverage_rule():
    prompt = llm.system_prompt(FAQ)
    assert "requirement or limit that explains" in prompt
    assert "When in doubt, answer CUSTOMER" in llm.SPAM_PROMPT


# ---- a reply may repeat the customer's own numbers, but never as a price ----


def checks_for(draft, ticket_text):
    from agent.decide import ModelOutput, reply_checks
    return reply_checks(ModelOutput(**covered_form(FAQ, draft_reply=draft)), FAQ, ticket_text)


def test_reply_may_repeat_a_number_from_the_ticket():
    ticket_text = "Mijn telefoon heeft versie 9 en de app start niet."
    draft = "Hoi, je telefoon heeft versie 9. Dat is het antwoord op je vraag. Groet, Team Roosterly"
    assert checks_for(draft, ticket_text)["facts"] is True
    assert checks_for(draft, "De app start niet.")["facts"] is False  # 9 is from nowhere


@pytest.mark.parametrize("price", ["€9", "9 euro", "9%", "9 procent"])
def test_a_ticket_number_can_never_become_a_price(price):
    ticket_text = "Wij hebben 9 medewerkers. Wat kost het?"
    draft = f"Hoi, dat kost {price} per maand. Dat is het antwoord op je vraag. Groet, Team Roosterly"
    assert checks_for(draft, ticket_text)["facts"] is False
