"""Production fixes: a slip by the model or the translation must not cost a good reply,
and every safety check stays as strict. Each test failed before its fix."""

import pytest

from agent import llm
from agent.decide import view_matches
from tests.fakes import FakeDesk, covered_form, fake_model
from tests.test_run import log_lines, start, tickets

FAQ = llm.load_faq()
HOWTO = "Hoe zet ik mijn wachtwoord opnieuw?"
GOOD = covered_form(FAQ)
WRONG_NAME = covered_form(FAQ, draft_reply='Hoi, klik op "Wachtwoord vergetenn" in de app. Groet, Team Roosterly')
PROMISE = covered_form(FAQ, draft_reply="Hoi, we bellen je morgen terug. Groet, Team Roosterly")


def forms_asked(calls):
    return [c for c in calls if c != "second spam check" and "<ticket_" in c]


# ---- B: one repair try for a draft that fails the fact or quote check ----


def test_a_wrong_menu_name_is_repaired_once(monkeypatch, clock, tmp_path):
    calls = fake_model(monkeypatch, lambda user: GOOD if "refused the draft" in user else WRONG_NAME)
    desk = FakeDesk(tickets(HOWTO))
    start(desk, clock, tmp_path)
    line = log_lines(tmp_path)[0]
    assert [action for _, action, _ in desk.posts] == ["reply"] and line["repaired"] is True
    assert len(forms_asked(calls)) == 2
    assert "Wachtwoord vergetenn" in forms_asked(calls)[1]  # the model is told what was wrong


def test_a_repair_that_fails_again_goes_to_a_human(monkeypatch, clock, tmp_path):
    calls = fake_model(monkeypatch, lambda user: WRONG_NAME)
    desk = FakeDesk(tickets(HOWTO))
    start(desk, clock, tmp_path)
    line = log_lines(tmp_path)[0]
    assert [action for _, action, _ in desk.posts] == ["handoff"]
    assert line["handoff_reason"].startswith("check_failed") and len(forms_asked(calls)) == 2


def test_a_promise_is_never_repaired(monkeypatch, clock, tmp_path):
    calls = fake_model(monkeypatch, lambda user: GOOD if "refused the draft" in user else PROMISE)
    desk = FakeDesk(tickets(HOWTO))
    start(desk, clock, tmp_path)
    assert [action for _, action, _ in desk.posts] == ["handoff"] and len(forms_asked(calls)) == 1


# ---- A: the English check translates once more before a handoff ----


def views(monkeypatch, *texts):
    """english_view returns these texts in turn."""
    asked = []

    def view(settings, draft, sleep=None):
        asked.append(draft)
        return texts[min(len(asked), len(texts)) - 1], 5, 5

    monkeypatch.setattr(llm, "english_view", view)
    return asked


def test_a_translation_slip_gets_a_second_translation(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: GOOD)
    asked = views(monkeypatch, "Hi, you can do that in the Basic app. Regards, Team Roosterly",
                  "Hi, you can do that in the app. Regards, Team Roosterly")
    desk = FakeDesk(tickets(HOWTO))
    start(desk, clock, tmp_path)
    assert [action for _, action, _ in desk.posts] == ["reply"] and len(asked) == 2


def test_two_failed_translations_go_to_a_human(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: GOOD)
    asked = views(monkeypatch, "Hi, you can do that in the Basic app. Regards, Team Roosterly")
    desk = FakeDesk(tickets(HOWTO))
    start(desk, clock, tmp_path)
    assert [action for _, action, _ in desk.posts] == ["handoff"] and len(asked) == 2


def test_a_promise_in_the_translation_is_not_translated_again(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: GOOD)
    asked = views(monkeypatch, "Hi, we will call you tomorrow. Regards, Team Roosterly",
                  "Hi, you can do that in the app. Regards, Team Roosterly")
    desk = FakeDesk(tickets(HOWTO))
    start(desk, clock, tmp_path)
    assert [action for _, action, _ in desk.posts] == ["handoff"] and len(asked) == 1


@pytest.mark.parametrize("draft, view, same", [
    ("Ihre beiden Mitarbeiter können das.", "Your two employees can do that.", True),
    ("De link is 60 minuten geldig.", "The link is valid for 30 minutes.", False),
    ("Zie app.roosterly.nl/login.", "See app.roosterly.nl.", False),
])
def test_the_translation_compare_uses_digits_and_links(draft, view, same):
    assert view_matches(draft, view) is same


def test_the_customer_plan_name_is_a_known_name(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: GOOD)
    views(monkeypatch, "Hi, on your Basis plan you can do that in the app. Regards, Team Roosterly")
    desk = FakeDesk(tickets(HOWTO))  # these tickets are on the Basis plan
    start(desk, clock, tmp_path)
    assert [action for _, action, _ in desk.posts] == ["reply"]


def test_an_english_draft_is_checked_as_it_is(monkeypatch, clock, tmp_path):
    english = covered_form(FAQ, language="en",
                           draft_reply="Hello, you can do that in the app. Kind regards, Team Roosterly")
    fake_model(monkeypatch, lambda user: english)
    asked = views(monkeypatch, "Hallo, dat kan in de app. Groet, Team Roosterly")
    desk = FakeDesk(tickets("How do I reset my password in the app please?"))
    start(desk, clock, tmp_path)
    line = log_lines(tmp_path)[0]
    assert [action for _, action, _ in desk.posts] == ["reply"] and asked == []
    assert line["english_view"].startswith("Hello")


def test_a_view_that_is_not_english_is_not_trusted(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: GOOD)
    asked = views(monkeypatch, "Hoi, dat kan in de app. Dat is het antwoord op je vraag. Groet, Team Roosterly")
    desk = FakeDesk(tickets(HOWTO))
    start(desk, clock, tmp_path)
    line = log_lines(tmp_path)[0]
    assert [action for _, action, _ in desk.posts] == ["handoff"] and len(asked) == 2
    assert "not_english" in line["handoff_reason"] or "not_english" in line["sent"]["reason"]
