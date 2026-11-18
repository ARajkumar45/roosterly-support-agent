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


# ---- one log line per ticket, also after a crash between "saved" and "logged" ----


def test_ticket_saved_before_a_crash_gets_its_log_line_on_restart(monkeypatch, clock, tmp_path):
    from tests.fakes import FakeDesk
    from tests.test_run import log_lines, start, tickets

    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets("Hoe werkt dit?", "Hoe werkt dat?"))
    desk.tickets["x1"]["handled"] = {"action": "reply"}  # saved, but the run died before logging
    code, text = start(desk, clock, tmp_path)
    lines = log_lines(tmp_path)
    assert code == 0 and "missing log line" in text
    assert sorted(line["ticket_id"] for line in lines) == ["x1", "x2"]  # one line each
    assert [l["outcome"] for l in lines if l["ticket_id"] == "x1"] == ["posted_found_on_restart"]
    assert [post[0] for post in desk.posts] == ["x2"]  # x1 was not posted again

    code, text = start(desk, clock, tmp_path)  # a third start adds nothing
    assert len(log_lines(tmp_path)) == 2 and "missing log line" not in text


def test_reset_moves_old_logs_aside(monkeypatch, clock, tmp_path):
    from agent.reset import reset
    from tests.fakes import FakeDesk
    from tests.test_run import log_lines, start, tickets

    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets("Hoe werkt dit?"))
    start(desk, clock, tmp_path)
    code = reset(make_settings(), desk.client(clock), final_marker=tmp_path / ".final",
                 say=lambda line: None, ask_user=lambda question: "reset", log_dir=tmp_path / "logs")
    assert code == 0 and log_lines(tmp_path) == []
    assert len(list((tmp_path / "logs").glob("before_reset_*/*.jsonl"))) == 1
    start(desk, clock, tmp_path)
    assert len(log_lines(tmp_path)) == 1  # the new cycle has its own single line


# ---- a mail with only a quoted line and a phone signature is empty ----


@pytest.mark.parametrize("signature", [
    "Verzonden vanaf mijn iPhone", "Verstuurd vanaf mijn Samsung", "Sent from my iPad",
    "Get Outlook for Android", "Von meinem iPhone gesendet",
])
def test_quote_and_signature_only_is_empty(signature):
    body = f"> Op dinsdag schreef iemand: hallo daar\n\n{signature}"
    assert guard("Re:", body).close_signal == "empty"
    assert guard("Re:", body + "\nWaar vind ik mijn rooster?").close_signal is None  # real text stays


def test_prompt_says_a_ticket_without_a_question_goes_to_a_human():
    assert "asks nothing" in llm.system_prompt(FAQ)


# ---- errors carry their reason, and the model is not asked again when it cannot help ----


def test_422_keeps_the_problems_list():
    import httpx
    from agent.helpdesk import Helpdesk
    desk = Helpdesk("https://fake.test", "k", "e@example.com", dry_run=False,
                    transport=httpx.MockTransport(lambda r: httpx.Response(
                        422, json={"error": "validation_failed", "problems": ["priority: invalid"]})))
    error = desk.error_for(desk.send("POST", "/tickets/x/reply", json={}))
    assert "priority: invalid" in str(error)


@pytest.mark.parametrize("status, calls", [(400, 1), (401, 1), (404, 1), (429, 3), (529, 3)])
def test_model_retries_only_what_can_succeed(status, calls):
    seen = []

    class Failure(Exception):
        status_code = status

    def provider(settings, model, system, user):
        seen.append(1)
        raise Failure()

    llm.PROVIDERS["failing"] = provider
    try:
        with pytest.raises(llm.ProviderDown):
            llm.ask(make_settings(provider="failing"), "m", "s", "u", sleep=lambda s: None)
    finally:
        del llm.PROVIDERS["failing"]
    assert len(seen) == calls


# ---- red-team findings: each attack that got through, now blocked ----

from agent.decide import ModelOutput, decide, reply_checks, view_problems  # noqa: E402
from agent.rules import has_payment_details, redact  # noqa: E402

ZW = "​"  # an invisible zero-width space


@pytest.mark.parametrize("written", [
    f"NL91{ZW}ABNA{ZW}0417{ZW}1643{ZW}00",
    f"4111{ZW}1111{ZW}1111{ZW}1111",
    "4111 1111-1111 1111",                         # mixed separators
    "4111_1111_1111_1111", "4111,1111,1111,1111",
    "NL91 ABNA 0417\n> 1643 00",                   # wrapped over two quoted lines
    "NL91\n      ABNA\n      0417\n      1643\n      00",  # table cells after HTML is removed
    "ＮＬ９１ＡＢＮＡ０４１７１６４３００",               # wide characters
])
def test_payment_details_hidden_in_odd_forms(written):
    hidden = redact(f"Mijn gegevens: {written} graag.")
    assert "0417" not in hidden and "1111" not in hidden and "[redacted]" in hidden
    assert has_payment_details(f"Uw kaart {written}.")


def test_redaction_still_leaves_normal_text_alone():
    for text in ("Bel 050 123 4567 of 06-12345678.", "Van 2026-11-18 tot 2026-12-01.",
                 "Laatste vier cijfers: 1111.", "Wij hebben\n> 12 medewerkers."):
        assert "[redacted]" not in redact(text)
        assert not has_payment_details(text)


def draft_checks(draft, ticket_text="Vraag"):
    return reply_checks(ModelOutput(**covered_form(FAQ, draft_reply=draft)), FAQ, ticket_text)


@pytest.mark.parametrize("draft, ticket_text", [
    ("Hoi, een extra medewerker kost acht euro per maand. Groet, Team Roosterly", "We hebben er nu 8."),
    ("Hoi, de link blijft 600 minuten geldig. Dat is het antwoord. Groet, Team Roosterly", "Is de link 600 minuten geldig?"),
    ("Hoi, dat kost € 9 per maand. Dat is het antwoord. Groet, Team Roosterly", "Wij zijn met 9."),
    ("Hoi, klik op 'Account Beheer' en kies je naam. Groet, Team Roosterly", "Account Beheer?"),
])
def test_invented_money_time_or_menu_fails_the_fact_check(draft, ticket_text):
    assert draft_checks(draft, ticket_text)["facts"] is False


@pytest.mark.parametrize("view", [
    "Hello, a colleague will then look at your account.",
    "Version 15 will be supported within 2 weeks.",
    "Our team will check this for you.",
    "This will be fixed in the next release.",
])
def test_promises_without_we_or_i_are_caught(view):
    assert "promise" in view_problems(view, view, view)


def test_plain_facts_in_the_future_tense_are_not_promises():
    view = "Hello, the employee then gets an email. The new price applies from the next invoice."
    assert "promise" not in view_problems(view, view, view)


def test_dutch_reply_to_a_german_ticket_is_stopped():
    body = "Hallo, ich habe mein Passwort vergessen. Was kann ich tun? Vielen Dank für die Hilfe."
    out = ModelOutput(**covered_form(FAQ, language="nl"))  # the model got the language wrong
    ticket = {"subject": "Passwort", "body": body, "customer_plan": "Basis"}
    decision = decide(ticket, guard("Passwort", body), out, FAQ, 0.75)
    assert decision.action == "handoff" and decision.handoff_reason == "language"


@pytest.mark.parametrize("body, topic", [
    ("Our lawyers will contact you.", "legal"), ("Mein Rechtsanwalt meldet sich.", "legal"),
    ("Has this been refunded yet?", "refund"), ("We were billed twice.", "refund"),
    ("Hoe kan ik opzeggen?", "cancel"), ("Please delete my account.", "cancel"),
    ("Someone logged into my account last night.", "security"),
])
def test_wordings_from_the_red_team_force_a_handoff(body, topic):
    assert topic in guard("Vraag", body).tier1


def test_a_broken_ticket_does_not_stop_the_run(monkeypatch, clock, tmp_path):
    from tests.fakes import FakeDesk
    from tests.test_run import log_lines, start, tickets

    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets("Hoe werkt dit?", "Hoe werkt dat?"))
    calls = {"n": 0}
    real = llm.build_user_message

    def explode_once(ticket):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TypeError("unexpected field type")
        return real(ticket)

    monkeypatch.setattr(llm, "build_user_message", explode_once)
    code, _ = start(desk, clock, tmp_path)
    outcomes = sorted(line["outcome"] for line in log_lines(tmp_path))
    assert code == 1 and outcomes == ["failed_left_open", "posted"]


def test_a_cut_off_log_line_does_not_block_a_start(tmp_path):
    from agent.logs import finished_ids
    (tmp_path / "run.jsonl").write_text('{"ticket_id": "x1", "outcome": "posted"}\n{"ticket_id": "x2", "outc')
    assert finished_ids(tmp_path) == {"x1"}



def test_a_time_limit_written_in_the_help_centre_is_not_a_promise():
    view = "If you do not receive an email within 10 minutes, check your spam folder."
    assert "promise" not in view_problems(view, view, view, faq_text=FAQ["F01"])
    assert "promise" in view_problems(view, view, view, faq_text=FAQ["F02"])  # not from this entry



# ---- two general rules the word list was missing (my own wordings, not ticket text) ----


@pytest.mark.parametrize("body", [
    "Werkt het morgen weer niet, dan zeg ik op.",
    "Nog een fout en wij zeggen op!",
    "Als het zo doorgaat zeggen we op, echt waar.",
])
def test_split_verb_cancel_without_an_object(body):
    assert "cancel" in guard("Vraag", body).tier1


@pytest.mark.parametrize("body", [
    "Er heeft vannacht iemand vanuit het buitenland op mijn beheerdersaccount ingelogd.",
    "Somebody we do not know logged into the manager account yesterday.",
    "Gestern hat sich jemand aus Spanien mit meinem Konto angemeldet.",
    "We zijn ingelogd door een onbekende partij.",
])
def test_someone_else_logged_in_is_a_security_case(body):
    assert "security" in guard("Vraag", body).tier1


@pytest.mark.parametrize("body", [
    "Ik kan niet inloggen, kan iemand mij helpen?",
    "Hoe zeg ik op welke dag ik vrij wil?",
    "Mijn collega zegt op het werk dat de app traag is.",
    "Ik ben ingelogd maar zie mijn rooster niet.",
])
def test_these_rules_leave_normal_questions_alone(body):
    assert not {"cancel", "security"} & set(guard("Vraag", body).tier1)
