"""Tests for problems an independent audit found. Each one is a general rule,
shown on a ticket written for the test."""

import httpx
import pytest
from pydantic import ValidationError

from agent import llm
from agent.decide import ModelOutput, decide
from agent.helpdesk import TicketError, build_body
from agent.rules import close_signal, guard, redact, strip_html
from agent.run import run
from eval.run_eval import wilson
from tests.fakes import FakeDesk, covered_form, fake_model, make_settings
from tests.test_run import HOWTO, log_lines, start, tickets

FAQ = llm.load_faq()

# ---- text between < and > is customer text, not HTML ----


def test_stray_angle_bracket_does_not_hide_a_refund():
    body = ("We hebben < 10 medewerkers. Ik wil mijn geld terug voor oktober.\n"
            "> Op maandag schreef iemand: hallo\nHoe voeg ik iemand toe?")
    assert "refund" in guard("Vraag", body).tier1
    message, _ = llm.build_user_message({"subject": "Vraag", "body": body})
    assert "geld terug" in message  # the model sees it too


def test_sentence_in_angle_brackets_is_not_empty():
    body = "<Ik wil mijn geld terug, anders stap ik naar de rechter>"
    assert close_signal("", body) is None
    assert "refund" in guard("", body).tier1


def test_real_html_is_still_removed():
    assert strip_html("<p>Hallo</p><br/><div class='x'>daar</div>").split() == ["Hallo", "daar"]
    assert "refund" in guard("", "Ik wil mijn geld<b> terug</b>").tier1


# ---- payment details with other separators ----


@pytest.mark.parametrize("written", [
    "NL91-ABNA-0417-1643-00", "NL91.ABNA.0417.1643.00", "NL91  ABNA  0417  1643  00",
    "NL 91 ABNA 0417 1643 00", "nl91abna0417164300", "DE89 3704 0044 0532 0130 00",
    "4111.1111.1111.1111", "4111  1111  1111  1111", "4111/1111/1111/1111",
    "4111 1111\n1111 1111", "3782 822463 10005",
])
def test_payment_details_are_hidden_however_written(written):
    hidden = redact(f"Betaal via {written} graag.")
    assert "[redacted]" in hidden
    assert "0417" not in hidden and "1111" not in hidden and "3704" not in hidden
    assert hidden.endswith("graag.")  # the words after it are kept


@pytest.mark.parametrize("text", [
    "Op 12 december 2026 komt mijn team weer terug van vakantie.",
    "Bel ons op 050 123 4567 of mail naar ons kantoor.",
    "We zijn in 20 minuten klaar met alle roosters voor deze week.",
])
def test_normal_text_is_not_redacted(text):
    assert redact(text) == text


# ---- decisions the helpdesk would reject must never be built ----


def form(**changes):
    return ModelOutput(**covered_form(FAQ, **changes))


def decide_for(body, out):
    ticket = {"subject": "Vraag", "body": body, "customer_plan": "Basis"}
    return decide(ticket, guard("Vraag", body), out, FAQ, 0.75)


def test_many_topics_still_give_a_valid_body():
    body = ("Ik wil mijn geld terug en mijn abonnement opzeggen. Verwijder mijn gegevens. "
            "Ons account is gehackt. Ik schakel mijn advocaat in en we stappen over.")
    decision = decide_for(body, form())
    assert len(guard("", body).tier1) == 6 and decision.priority == "P1"
    assert len(build_body("handoff", decision.fields())["reason"]) <= 300


def test_confidence_out_of_range_is_an_invalid_form():
    with pytest.raises(ValidationError):
        form(confidence=85)  # so the model gets its one repair try


def test_empty_model_note_and_reason_are_replaced():
    decision = decide_for(HOWTO, form(action="handoff", draft_reply=None, handoff_note=" ", reason=""))
    body = build_body("handoff", decision.fields())
    assert body["handoff_note"].startswith("Customer wants:") and body["reason"]


def test_blank_draft_is_not_sent():
    assert decide_for(HOWTO, form(draft_reply="   \n")).action == "handoff"


def test_too_long_ticket_goes_to_a_human():
    body = HOWTO + " woord" * 1500  # the model would not see the middle
    decision = decide_for(body, form())
    assert decision.action == "handoff" and decision.handoff_reason == "too_long"


# ---- runner ----


def test_left_open_ticket_gets_a_redacted_log_line(monkeypatch, clock, tmp_path, capsys):
    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets(HOWTO))
    helpdesk = desk.client(clock)

    def rejected(ticket_id, action, fields):
        raise TicketError(422, "bad value NL91 ABNA 0417 1643 00")

    monkeypatch.setattr(helpdesk, "post_action", rejected)
    code = run(make_settings(), helpdesk, False, assume_yes=True, log_dir=tmp_path / "logs",
               marker=tmp_path / ".agent_account", final_marker=tmp_path / ".final",
               say=lambda line: None)
    line = log_lines(tmp_path)[0]
    assert code == 1 and line["outcome"] == "failed_left_open"
    assert "NL91" not in line["error"] and "NL91" not in capsys.readouterr().err


def test_no_real_run_after_the_final_run(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets(HOWTO))
    marker = tmp_path / ".final_run_done"
    marker.write_text("")
    code, text = start(desk, clock, tmp_path, final_marker=marker)
    assert code == 2 and desk.posts == [] and "final run" in text
    code, _ = start(desk, clock, tmp_path, dry_run=True, final_marker=marker)
    assert code == 0  # a dry run is still allowed


def test_wrong_url_gives_a_message_not_a_crash(clock, tmp_path):
    desk = FakeDesk(tickets(HOWTO))
    desk.handler = lambda request: httpx.Response(404, json={"error": "not_found"})
    code, text = start(desk, clock, tmp_path, dry_run=True)
    assert code == 2 and "HELPDESK_URL" in text


def test_wilson_interval_is_wide_for_ten_examples():
    low, high = wilson(8, 10)
    assert 0.48 < low < 0.50 and 0.93 < high < 0.95
