"""End-to-end tests of the runner against a fake helpdesk and a fake model."""

import json
from types import SimpleNamespace

import httpx
import pytest

from agent import llm
from agent.reset import reset
from agent.run import run
from eval.run_eval import grade
from tests.fakes import FakeDesk, covered_form, fake_model, make_settings

FAQ = llm.load_faq()
HOWTO = "Waar in de app zet ik een collega op de planning?"
REFUND = "Ik wil mijn geld terug. Mijn rekening is NL91 ABNA 0417 1643 00."


def tickets(*bodies):
    return [
        {"id": f"x{n}", "received_at": f"2026-01-01T00:00:0{n}", "subject": "Vraag",
         "body": body, "customer_plan": "Basis", "channel": "email"}
        for n, body in enumerate(bodies, start=1)
    ]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)


def start(desk, clock, tmp_path, dry_run=False, settings=None, **more):
    lines = []
    code = run(
        settings or make_settings(), desk.client(clock, dry_run), dry_run,
        assume_yes=True, log_dir=tmp_path / "logs", marker=tmp_path / ".agent_account",
        say=lines.append, **more,
    )
    return code, "\n".join(lines)


def log_lines(tmp_path, folder="logs"):
    return [json.loads(line) for file in sorted((tmp_path / folder).glob("*.jsonl"))
            for line in file.read_text().splitlines()]


def test_dry_run_never_posts(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets(HOWTO, REFUND))
    code, _ = start(desk, clock, tmp_path, dry_run=True)
    assert code == 0 and desk.posts == []
    lines = log_lines(tmp_path, "logs/dry")
    assert [line["outcome"] for line in lines] == ["dry_run", "dry_run"]


def test_one_post_and_one_line_per_ticket(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets(HOWTO, REFUND, HOWTO))
    code, text = start(desk, clock, tmp_path)
    assert code == 0
    assert [(ticket_id, action) for ticket_id, action, _ in desk.posts] == [
        ("x1", "reply"), ("x2", "handoff"), ("x3", "reply")]
    lines = log_lines(tmp_path)
    assert [line["ticket_id"] for line in lines] == ["x1", "x2", "x3"]
    assert "409 count: 0" in text


def test_english_view_present_for_reply(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    start(FakeDesk(tickets(HOWTO, REFUND)), clock, tmp_path)
    reply, handoff = log_lines(tmp_path)
    assert reply["english_view"] and reply["checks"]["english_view"] is True
    assert handoff["english_view"] is None


def test_log_line_redacted(monkeypatch, clock, tmp_path):
    leaky = covered_form(FAQ, summary="Wants money back to NL91 ABNA 0417 1643 00.")
    fake_model(monkeypatch, lambda user: leaky)
    desk = FakeDesk(tickets(REFUND))
    start(desk, clock, tmp_path)
    logged = (tmp_path / "logs").glob("*.jsonl").__next__().read_text()
    assert "NL91" not in logged and "body" not in json.loads(logged)
    assert "NL91" not in json.dumps(desk.posts[0][2])  # nor in what we posted


def test_crash_then_rerun(monkeypatch, clock, tmp_path):
    seen = []

    def crash_on_third(user):
        seen.append(user)
        if len(seen) == 3:
            raise KeyboardInterrupt
        return covered_form(FAQ)

    fake_model(monkeypatch, crash_on_third)
    desk = FakeDesk(tickets(HOWTO, HOWTO, HOWTO, HOWTO))
    code, text = start(desk, clock, tmp_path)
    assert code == 1 and "interrupted" in text and len(desk.posts) == 2

    code, text = start(desk, clock, tmp_path)  # the re-run reads what is still open
    assert code == 0 and "409 count: 0" in text
    assert sorted(ticket_id for ticket_id, _, _ in desk.posts) == ["x1", "x2", "x3", "x4"]


def test_timeout_after_save_is_not_posted_twice(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets(HOWTO))

    def saved_but_ok_lost(request, ticket_id, save):
        save()
        raise httpx.ReadTimeout("slow", request=request)

    desk.post_hook = saved_but_ok_lost
    code, _ = start(desk, clock, tmp_path)
    assert code == 0 and len(desk.posts) == 1
    assert log_lines(tmp_path)[0]["outcome"] == "posted_confirmed_by_get"


def test_failed_ticket_exit_1(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets(HOWTO, HOWTO))

    def reject_first(request, ticket_id, save):
        if ticket_id == "x1":
            return httpx.Response(422, json={"error": "validation_failed"})
        return save()

    desk.post_hook = reject_first
    code, text = start(desk, clock, tmp_path)
    assert code == 1 and "left open: 1" in text
    assert [ticket_id for ticket_id, _, _ in desk.posts] == ["x2"]  # the run went on


def test_401_stops_run(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets(HOWTO))
    desk.handler = lambda request: httpx.Response(401, json={"error": "unauthorized"})
    code, _ = start(desk, clock, tmp_path)
    assert code == 2 and desk.posts == []


def test_provider_down_leaves_open(monkeypatch, clock, tmp_path, capsys):
    def broken(settings, model, system, user):
        raise ConnectionError("no network")

    monkeypatch.setitem(llm.PROVIDERS, "fake", broken)
    desk = FakeDesk(tickets(REFUND, HOWTO, HOWTO, HOWTO))
    code, text = start(desk, clock, tmp_path)
    assert code == 1 and desk.posts == []  # never a guess
    assert "3 tickets in a row" in text
    assert "NL91" not in capsys.readouterr().err  # test_error_text_redacted


def test_model_failure_hands_off_by_rule(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: "not a form")
    desk = FakeDesk(tickets(REFUND))
    code, _ = start(desk, clock, tmp_path)
    ticket_id, action, body = desk.posts[0]
    assert code == 0 and action == "handoff" and body["priority"] == "P2"


def test_real_run_needs_a_model_and_a_yes(monkeypatch, clock, tmp_path):
    desk = FakeDesk(tickets(HOWTO))
    code, text = start(desk, clock, tmp_path, settings=make_settings(provider=""))
    assert code == 2 and "No model" in text
    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    code = run(make_settings(), desk.client(clock), False, log_dir=tmp_path,
               marker=tmp_path / ".agent_account", say=lambda line: None,
               ask_user=lambda question: "n")
    assert code == 2 and desk.posts == []


def test_rules_only_dry_run_works_without_a_model(clock, tmp_path):
    desk = FakeDesk(tickets(HOWTO, REFUND))
    code, text = start(desk, clock, tmp_path, dry_run=True, settings=make_settings(provider=""))
    assert code == 0 and "'handoff': 2" in text


def test_other_email_is_refused(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets(HOWTO))
    start(desk, clock, tmp_path, dry_run=True)
    other = make_settings(email="someone-else@example.com")
    code, text = start(desk, clock, tmp_path, dry_run=True, settings=other)
    assert code == 2 and "differs" in text


# ---- reset ----


def do_reset(desk, clock, tmp_path, word="reset"):
    return reset(make_settings(), desk.client(clock), final_marker=tmp_path / ".final_run_done",
                 say=lambda line: None, ask_user=lambda question: word,
                 log_dir=tmp_path / "logs")  # never the real logs folder


def test_reset_reopens_tickets(monkeypatch, clock, tmp_path):
    fake_model(monkeypatch, lambda user: covered_form(FAQ))
    desk = FakeDesk(tickets(HOWTO))
    start(desk, clock, tmp_path)
    assert do_reset(desk, clock, tmp_path) == 0
    assert desk.tickets["x1"]["handled"] is None


def test_reset_refusals(clock, tmp_path):
    desk = FakeDesk(tickets(HOWTO))
    desk.tickets["x1"]["handled"] = {"action": "reply"}
    assert do_reset(desk, clock, tmp_path, word="yes") == 2  # wrong word
    assert do_reset(FakeDesk([], mode="live"), clock, tmp_path) == 2  # not practice mode
    (tmp_path / ".final_run_done").write_text("")
    assert do_reset(desk, clock, tmp_path) == 2  # after the final run
    assert desk.tickets["x1"]["handled"] is not None  # nothing was re-opened


# ---- eval grading ----


def test_grade_counts_and_safety():
    def label(action, priority="P4"):
        return {"language": "nl", "category": "question", "priority": priority, "action": action}

    examples = [{"id": "a", "label": label("handoff", "P2")}, {"id": "b", "label": label("reply")},
                {"id": "c", "label": label("reply")}]
    ours = [SimpleNamespace(**label("reply"), handoff_reason=None),      # unsafe: should be handoff
            SimpleNamespace(**label("handoff"), handoff_reason="model"),  # answerable, handed off
            SimpleNamespace(**label("reply"), handoff_reason=None)]
    rows, counts, unsafe, missed = grade(examples, ours)
    assert counts == {"language": 3, "category": 3, "priority": 2, "action": 1}
    assert unsafe == ["a"] and missed == ["b"]
