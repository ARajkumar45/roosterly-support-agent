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

from pydantic import ValidationError

from agent import config, llm
from agent.decide import decide, to_handoff, view_problems
from agent.helpdesk import GaveUp, Helpdesk, HelpdeskError, StopRun, TicketError, build_body
from agent.logs import write_line
from agent.rules import guard, redact

MAX_PROVIDER_FAILURES = 3  # this many tickets in a row without a model answer: stop


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def commit_hash():
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=config.ROOT, check=True,
        ).stdout.strip()
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

    view = None
    if decision.action == "reply":
        view, more_in, more_out = llm.english_view(settings, decision.draft_reply)
        tokens_in, tokens_out = tokens_in + more_in, tokens_out + more_out
        known = " ".join([*(faq.get(i, "") for i in decision.faq_ids),
                          str(ticket.get("subject")), str(ticket.get("body"))])
        problems = view_problems(decision.draft_reply, view, known)
        decision.checks["english_view"] = not problems
        if problems:
            decision = to_handoff(
                decision, "check_failed:english_view",
                f"The English check of the draft reply failed ({', '.join(problems)}).",
                out.summary, ticket.get("customer_plan") or "unknown",
            )
    return decision, found, view, tokens_in, tokens_out


def check_account(email, marker):
    """Remember which email this repo runs as, and refuse a different one later."""
    digest = hashlib.sha256(email.encode()).hexdigest()
    if marker.exists():
        return marker.read_text().strip() == digest
    marker.write_text(digest + "\n")
    return True


def run(settings, helpdesk, dry_run, tickets=None, assume_yes=False,
        log_dir=config.ROOT / "logs", marker=config.ROOT / ".agent_account",
        final_marker=config.ROOT / ".final_run_done", say=print, ask_user=input):
    """Returns the exit code: 0 all done, 1 some tickets left open, 2 cannot run."""
    if settings.email.count("@") != 1:
        say("CANDIDATE_EMAIL is missing or not an email address. Check .env.")
        return 2
    if not check_account(settings.email, marker):
        say("CANDIDATE_EMAIL differs from the one used before in this repo. Refusing to run.")
        return 2
    if not dry_run and final_marker.exists():
        say("The final run is done (.final_run_done exists). A real run is refused.")
        return 2
    if not dry_run and not settings.provider:
        say("No model is set (LLM_PROVIDER). A real run needs one. Use --dry-run for rules only.")
        return 2

    run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = (log_dir / "dry" if dry_run else log_dir) / f"{run_id}.jsonl"
    model = settings.model or "none (rules only)"
    try:
        if tickets is None:
            status = helpdesk.status(settings.name)
            say(f"Account {mask(settings.email)}  mode {status.get('mode')}  "
                f"open {status.get('open_tickets')}  handled {status.get('handled_tickets')}")
            tickets = helpdesk.list_tickets("all" if dry_run else "open")
            if helpdesk.warning:
                say(f"WARNING: {helpdesk.warning}. The ticket list may be incomplete.")
        say(f"{run_id}  commit {commit_hash()}  model {model}  collected {len(tickets)} ids  "
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
    counts, reasons, outcomes, failed = Counter(), Counter(), Counter(), []

    def left_open(ticket_id, error, began):
        """A ticket we could not finish still gets its log line. A re-run picks it up."""
        failed.append(ticket_id)
        problem = f"{type(error).__name__} {redact(str(error))[:200]}"
        print(f"{ticket_id} left open: {problem}", file=sys.stderr)
        write_line(log_path, {
            "run_id": run_id, "ticket_id": ticket_id, "ts": utc_now(),
            "outcome": "failed_left_open", "error": problem,
            "latency_ms": round((time.monotonic() - began) * 1000),
            "model": model, "commit": commit_hash(),
        })
    cost, provider_failures, stopped, started = 0.0, 0, "", time.monotonic()
    try:
        for ticket in tickets:
            ticket_id, began = ticket["id"], time.monotonic()
            try:
                decision, found, view, tokens_in, tokens_out = triage(ticket, settings, faq)
                provider_failures = 0
                if dry_run:
                    build_body(decision.action, decision.fields())  # still check the body
                    outcome = "dry_run"
                else:
                    outcome = helpdesk.post_action(ticket_id, decision.action, decision.fields())
            except StopRun as error:
                say(f"The helpdesk refused us ({error}). Stopping.")
                return 2
            except llm.ProviderDown as error:
                provider_failures += 1
                left_open(ticket_id, error, began)
                if provider_failures >= MAX_PROVIDER_FAILURES:
                    stopped = "the model failed on 3 tickets in a row"
                    break
                continue
            except (GaveUp, TicketError, ValidationError) as error:
                left_open(ticket_id, error, began)
                continue

            ticket_cost = config.cost_eur(settings.model, tokens_in, tokens_out)
            cost += ticket_cost
            write_line(log_path, {
                "run_id": run_id, "ticket_id": ticket_id, "ts": utc_now(),
                "language": decision.language, "category": decision.category,
                "priority": decision.priority, "action": decision.action,
                "reason": decision.reason, "faq_ids": decision.faq_ids,
                "confidence": decision.confidence,
                "guard": {"tier1": found.tier1, "tier2": found.tier2,
                          "injection": found.injection, "close_signal": found.close_signal},
                "handoff_reason": decision.handoff_reason, "checks": decision.checks,
                "english_view": view, "outcome": outcome,
                "latency_ms": round((time.monotonic() - began) * 1000),
                "tokens": {"in": tokens_in, "out": tokens_out},
                "cost_eur": round(ticket_cost, 6), "model": model, "commit": commit_hash(),
            })
            counts[decision.action] += 1
            outcomes[outcome] += 1
            if decision.handoff_reason:
                reasons[decision.handoff_reason.split(":")[0]] += 1
            say(f"{ticket_id}  {decision.action:8} {decision.category:15} {decision.priority}  "
                f"{redact(decision.reason)[:90]}")
            if cost > settings.spend_cap_eur:
                stopped = "the spend cap was reached (raise SPEND_CAP_EUR to continue)"
                break
    except KeyboardInterrupt:
        stopped = "interrupted by the user"

    done = sum(counts.values())
    say(f"\nSummary {run_id}: {done} of {len(tickets)} tickets decided in "
        f"{time.monotonic() - started:.0f} s, cost EUR {cost:.4f}")
    say(f"  actions: {dict(counts)}")
    say(f"  handoff reasons: {dict(reasons)}  "
        f"handoff rate: {counts['handoff']}/{done or 1}")
    say(f"  outcomes: {dict(outcomes)}  409 count: {outcomes['already_handled_409']}")
    say(f"  left open: {len(tickets) - done} {failed if failed else ''}")
    if stopped:
        say(f"  stopped early: {stopped}. Run again to finish the open tickets.")
    if done or failed:
        say(f"  log: {log_path}")
    return 1 if (failed or stopped) else 0


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
    return run(settings, helpdesk, dry_run, tickets, args.yes)


if __name__ == "__main__":
    sys.exit(main())
