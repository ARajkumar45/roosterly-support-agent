"""Run the agent: read tickets, decide, post one decision each, log one line each.

  python -m agent.run --dry-run            reads everything, never posts
  python -m agent.run                      real run, asks for 'y' (or pass --yes)
  python -m agent.run --tickets-file f     dry-run on tickets from a .jsonl file

The helpdesk is the source of truth. Each start reads the open tickets again,
so after a crash a second run only handles what is still open. Run one
instance at a time.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone

from agent import config, llm
from agent.decide import decide, detect, to_handoff, view_problems
from agent.helpdesk import GaveUp, Helpdesk, HelpdeskError, StopRun, build_body
from agent.logs import finished_ids, write_line
from agent.rules import guard, redact

MAX_PROVIDER_FAILURES = 3  # this many tickets in a row without a model answer: stop


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def commit_hash():
    """The commit the code comes from, with "-dirty" when tracked files differ from it."""
    try:
        def git(*args):
            return subprocess.run(["git", *args], capture_output=True, text=True,
                                  cwd=config.ROOT, check=True).stdout.strip()
        changed = git("status", "--porcelain", "--untracked-files=no", "--", "agent", "eval/run_eval.py")
        return git("rev-parse", "--short", "HEAD") + ("-dirty" if changed else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def mask(email):
    name, _, domain = email.partition("@")
    return f"{name[:2]}***@{domain}"


def triage(ticket, settings, faq):
    """One ticket through guard, model, decision and the English check.

    Returns (decision, guard result, english view, tokens in, tokens out).
    Raises llm.ProviderDown when the model cannot be reached.
    """
    found = guard(ticket.get("subject"), ticket.get("body"))
    out, tokens_in, tokens_out = None, 0, 0
    if settings.provider:
        out, tokens_in, tokens_out, lookalike = llm.fill_form(settings, faq, ticket)
        found.injection = found.injection or lookalike
        # Close needs two signals. When the code has no spam signal of its own, a second,
        # separate model call may confirm spam. Never when any guard hint fired.
        clean = not (found.tier1 or found.tier2 or found.injection or found.close_signal)
        if out is not None and out.action == "close" and clean:
            is_spam, more_in, more_out = llm.second_spam_check(settings, ticket)
            tokens_in, tokens_out = tokens_in + more_in, tokens_out + more_out
            if is_spam:
                found.close_signal = "spam (confirmed by a second check)"
    decision = decide(ticket, found, out, faq, settings.threshold)

    # One repair try: only when the fact or quote check stopped the draft. The new form goes
    # through every rule and check again; if it fails again, the ticket goes to a human.
    if decision.repair_hint and settings.provider:
        again, more_in, more_out, _ = llm.fill_form(settings, faq, ticket, feedback=decision.repair_hint)
        tokens_in, tokens_out = tokens_in + more_in, tokens_out + more_out
        if again is not None:
            out = again
            decision = decide(ticket, found, out, faq, settings.threshold)
            decision.repaired = True

    view = None
    if decision.action == "reply":
        view, problems, more_in, more_out = english_check(settings, decision, ticket, faq)
        tokens_in, tokens_out = tokens_in + more_in, tokens_out + more_out
        decision.checks["english_view"] = not problems
        if problems:
            decision = to_handoff(
                decision, "check_failed:english_view",
                f"The English check of the draft reply failed ({', '.join(problems)}).",
                out.summary, ticket.get("customer_plan") or "unknown",
            )
    return decision, found, view, tokens_in, tokens_out


# Problems that can be a slip of the translation itself. A promise is never one of them:
# a second translation could word a real promise differently and hide it.
TRANSLATION_SLIPS = {"unknown_name", "numbers_or_links", "not_english"}


def english_check(settings, decision, ticket, faq):
    """Translate the draft back to English and check it. Returns (view, problems, tokens in, out).

    An English draft is checked as it is. Otherwise, when the only problems look like a
    translation slip (a name translated, a number written differently, a view that is not
    English), the draft is translated once more; the reply stands only if that one passes.
    """
    # Names the reply may use: the quoted help-centre entries, the ticket, and the plan the
    # helpdesk gives for this customer.
    known = " ".join([*(faq.get(i, "") for i in decision.faq_ids), str(ticket.get("subject")),
                      str(ticket.get("body")), str(ticket.get("customer_plan") or "")])
    cited = " ".join(faq.get(i, "") for i in decision.faq_ids)
    language, lead = detect(decision.draft_reply)
    if language == "en" and lead >= 2:
        view = decision.draft_reply
        return view, view_problems(decision.draft_reply, view, known, cited), 0, 0
    tokens_in = tokens_out = 0
    for _ in range(2):
        view, more_in, more_out = llm.english_view(settings, decision.draft_reply)
        tokens_in, tokens_out = tokens_in + more_in, tokens_out + more_out
        problems = view_problems(decision.draft_reply, view, known, cited)
        view_language, view_lead = detect(view)
        if view_language != "en" and view_lead >= 2:  # a promise check on Dutch text finds nothing
            problems.append("not_english")
        if not problems or not set(problems) <= TRANSLATION_SLIPS:
            break
    return view, problems, tokens_in, tokens_out


def check_account(email, marker):
    """Remember which email this repo runs as, and refuse a different one later."""
    digest = hashlib.sha256(email.encode()).hexdigest()
    if marker.exists():
        return marker.read_text().strip() == digest
    marker.write_text(digest + "\n")
    return True


class Run:
    """One run: its id, its log file and its counts."""

    def __init__(self, settings, dry_run, log_dir, say):
        self.settings, self.dry_run, self.say = settings, dry_run, say
        self.id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.log_path = (log_dir / "dry" if dry_run else log_dir) / f"{self.id}.jsonl"
        self.model = settings.model or "none (rules only)"
        self.counts, self.reasons, self.outcomes, self.failed = Counter(), Counter(), Counter(), []
        self.cost, self.stopped, self.started = 0.0, "", time.monotonic()
        self.provider_failures = 0  # tickets in a row without a model answer

    def log(self, ticket_id, began=None, **fields):
        record = {"run_id": self.id, "ticket_id": ticket_id, "ts": utc_now(), **fields}
        if began is not None:
            record["latency_ms"] = round((time.monotonic() - began) * 1000)
        write_line(self.log_path, {**record, "model": self.model, "commit": commit_hash()})

    def left_open(self, ticket_id, error, began):
        """A ticket we could not finish still gets its log line. A re-run picks it up."""
        self.failed.append(ticket_id)
        problem = f"{type(error).__name__} {redact(str(error))[:200]}"
        print(f"{ticket_id} left open: {problem}", file=sys.stderr)
        self.log(ticket_id, began, outcome="failed_left_open", error=problem)


def refuse_to_start(settings, dry_run, marker, final_marker):
    """A reason not to run at all, or None."""
    if settings.email.count("@") != 1:
        return "CANDIDATE_EMAIL is missing or not an email address. Check .env."
    if not check_account(settings.email, marker):
        return "CANDIDATE_EMAIL differs from the one used before in this repo. Refusing to run."
    if not dry_run and final_marker.exists():
        return "The final run is done (.final_run_done exists). A real run is refused."
    if not dry_run and not settings.provider:
        return "No model is set (LLM_PROVIDER). A real run needs one. Use --dry-run for rules only."
    return None


def collect(run, helpdesk, log_dir):
    """Read the tickets to handle. A real run also writes any log line a crash missed."""
    status = helpdesk.status(run.settings.name)
    run.say(f"Account {mask(run.settings.email)}  mode {status.get('mode')}  "
            f"open {status.get('open_tickets')}  handled {status.get('handled_tickets')}")
    tickets = helpdesk.list_tickets("all" if run.dry_run else "open")
    if helpdesk.warning:
        run.say(f"WARNING: {helpdesk.warning}. The ticket list may be incomplete.")
    if not run.dry_run:
        # A crash can fall between "decision saved" and "log line written".
        # Such a ticket is handled in the helpdesk but has no line. Give it one now.
        open_ids = {ticket["id"] for ticket in tickets}
        missing = [t["id"] for t in helpdesk.list_tickets("all")
                   if t["id"] not in open_ids and t["id"] not in finished_ids(log_dir)]
        for ticket_id in missing:
            run.log(ticket_id, outcome="posted_found_on_restart",
                    note="Handled in the helpdesk by an earlier run that stopped before "
                         "it could write this line.")
        if missing:
            run.say(f"Wrote {len(missing)} missing log line(s) for tickets handled before a crash.")
    return tickets


def handle(run, helpdesk, ticket, faq):
    """Decide one ticket, post it (unless dry run) and write its log line."""
    began = time.monotonic()
    ticket_id = ticket.get("id", "unknown") if isinstance(ticket, dict) else "unknown"
    decision, found, view, tokens_in, tokens_out = triage(ticket, run.settings, faq)
    run.provider_failures = 0  # the model answered
    body = build_body(decision.action, decision.fields())  # what is (or would be) sent
    if run.dry_run:
        outcome = "dry_run"
    else:
        outcome = helpdesk.post_action(ticket_id, decision.action, decision.fields())

    ticket_cost = config.cost_eur(run.settings.model, tokens_in, tokens_out)
    run.cost += ticket_cost
    run.log(
        ticket_id, began,
        language=decision.language, category=decision.category,
        priority=decision.priority, action=decision.action,
        reason=decision.reason, faq_ids=decision.faq_ids, confidence=decision.confidence,
        model_confidence=decision.model_confidence,  # also on handoffs, to test the cut-off
        rejected_draft=decision.rejected_draft,  # a draft a check stopped, to see why
        repaired=decision.repaired,  # the reply came from the one repair try
        guard={"tier1": found.tier1, "tier2": found.tier2,
               "injection": found.injection, "close_signal": found.close_signal},
        handoff_reason=decision.handoff_reason, checks=decision.checks,
        sent=body,  # the exact decision body, so every posted text can be audited
        english_view=view, outcome=outcome,
        tokens={"in": tokens_in, "out": tokens_out}, cost_eur=round(ticket_cost, 6),
    )
    run.counts[decision.action] += 1
    run.outcomes[outcome] += 1
    if decision.handoff_reason:
        run.reasons[decision.handoff_reason.split(":")[0]] += 1
    run.say(f"{ticket_id}  {decision.action:8} {decision.category:15} {decision.priority}  "
            f"{redact(decision.reason)[:90]}")


def summary(run, total):
    done = sum(run.counts.values())
    say = run.say
    say(f"\nSummary {run.id}: {done} of {total} tickets decided in "
        f"{time.monotonic() - run.started:.0f} s, cost EUR {run.cost:.4f}")
    say(f"  actions: {dict(run.counts)}")
    say(f"  handoff reasons: {dict(run.reasons)}  handoff rate: {run.counts['handoff']}/{done or 1}")
    say(f"  outcomes: {dict(run.outcomes)}  409 count: {run.outcomes['already_handled_409']}")
    say(f"  left open: {total - done} {run.failed if run.failed else ''}")
    if run.stopped:
        say(f"  stopped early: {run.stopped}. Run again to finish the open tickets.")
    if done or run.failed:
        say(f"  log: {run.log_path}")
    return 1 if (run.failed or run.stopped) else 0


def run(settings, helpdesk, dry_run, tickets=None, assume_yes=False, *,
        log_dir, marker, final_marker, say=print, ask_user=input):
    """Returns the exit code: 0 all done, 1 some tickets left open, 2 cannot run.

    log_dir, marker and final_marker have no defaults on purpose: only main() points them
    at the real repo, so a test can never read the real lock file by accident.
    """
    refusal = refuse_to_start(settings, dry_run, marker, final_marker)
    if refusal:
        say(refusal)
        return 2

    current = Run(settings, dry_run, log_dir, say)
    try:
        if tickets is None:
            tickets = collect(current, helpdesk, log_dir)
        say(f"{current.id}  commit {commit_hash()}  model {current.model}  collected {len(tickets)} ids  "
            f"spend cap EUR {settings.spend_cap_eur:.2f}")
    except StopRun as error:
        say(f"The helpdesk refused us ({error}). Check the key and email in .env.")
        return 2
    except GaveUp as error:
        say(f"The helpdesk did not answer ({error}). Try again in a minute.")
        return 1
    except HelpdeskError as error:
        say(f"The helpdesk rejected the request ({error}). Check HELPDESK_URL in .env.")
        return 2

    if not dry_run and not assume_yes:
        question = f"REAL RUN: will post to {len(tickets)} open tickets for {mask(settings.email)}. Type y: "
        if ask_user(question).strip().lower() != "y":
            say("Stopped. Nothing was posted.")
            return 2

    faq = llm.load_faq()
    try:
        for ticket in tickets:
            began = time.monotonic()
            ticket_id = ticket.get("id", "unknown") if isinstance(ticket, dict) else "unknown"
            try:
                handle(current, helpdesk, ticket, faq)
            except StopRun as error:
                say(f"The helpdesk refused us ({error}). Stopping.")
                return 2
            except llm.ProviderDown as error:
                current.provider_failures += 1
                current.left_open(ticket_id, error, began)
                if current.provider_failures >= MAX_PROVIDER_FAILURES:
                    current.stopped = "the model failed on 3 tickets in a row"
                    break
                continue
            except Exception as error:  # anything else: this ticket stays open, the run goes on
                current.left_open(ticket_id, error, began)
                continue
            if current.cost > settings.spend_cap_eur:
                current.stopped = "the spend cap was reached (raise SPEND_CAP_EUR to continue)"
                break
    except KeyboardInterrupt:
        current.stopped = "interrupted by the user"
    return summary(current, len(tickets))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Roosterly support ticket agent")
    parser.add_argument("--dry-run", action="store_true", help="read and decide, never post")
    parser.add_argument("--yes", action="store_true", help="skip the real-run question")
    parser.add_argument("--tickets-file", help="dry-run on tickets from a .jsonl file")
    args = parser.parse_args(argv)

    settings = config.load()
    dry_run = args.dry_run or bool(args.tickets_file)
    tickets = None
    if args.tickets_file:
        with open(args.tickets_file) as file:
            tickets = [json.loads(line) for line in file if line.strip()]
    helpdesk = Helpdesk(settings.helpdesk_url, settings.helpdesk_key, settings.email, dry_run)
    return run(settings, helpdesk, dry_run, tickets, args.yes, log_dir=config.ROOT / "logs",
               marker=config.ROOT / ".agent_account", final_marker=config.ROOT / ".final_run_done")


if __name__ == "__main__":
    sys.exit(main())
