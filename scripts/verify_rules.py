"""Check Nordavo's written rules against the repo and the latest run log, one by one.

  python -m scripts.verify_rules              uses the newest log in logs/ (or logs/dry/)
  python -m scripts.verify_rules --log FILE   uses one log file

It only reads. It calls the helpdesk with GET only (status, tickets, examples), never the
model, never a POST. Each line is PASS, FAIL or TODO with the evidence. Do not trust a
summary; read the lines.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from agent import config, llm
from agent.decide import detect
from agent.helpdesk import Helpdesk
from agent.rules import redact

ROOT = config.ROOT
results = []


def check(rule, passed, evidence, todo=False):
    status = "TODO" if todo else ("PASS" if passed else "FAIL")
    results.append(status)
    print(f"{status}  {rule}\n      {evidence}")


def tracked_files():
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=ROOT).stdout
    return [ROOT / name for name in out.split()]


def spans(text, size=6):
    """Every run of `size` words, lower case, for spotting copied text."""
    words = re.findall(r"\w+", (text or "").lower())
    return {" ".join(words[i:i + size]) for i in range(len(words) - size + 1)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", help="one run log file")
    args = parser.parse_args()

    settings = config.load()
    helpdesk = Helpdesk(settings.helpdesk_url, settings.helpdesk_key, settings.email, dry_run=True)
    status = helpdesk.status()
    all_tickets = helpdesk.list_tickets("all")
    examples = helpdesk.examples()
    faq = llm.load_faq()

    # A real cycle can span several log files (a run, a stop, a re-run). The reset moves
    # older cycles aside, so every run log directly in logs/ belongs to this cycle.
    if args.log:
        log_files = [Path(args.log)]
    else:
        log_files = sorted((ROOT / "logs").glob("run-*.jsonl")) or \
            sorted((ROOT / "logs" / "dry").glob("run-*.jsonl"))[-1:]
    log_text = "".join(file.read_text() for file in log_files)
    lines = [json.loads(line) for line in log_text.splitlines() if line.strip()]
    final = [line for line in lines if line.get("outcome") != "failed_left_open"]
    print(f"Logs: {', '.join(file.name for file in log_files)}  ({len(lines)} lines)\n")

    print("== 03 What to build ==")
    ids = [line["ticket_id"] for line in final]
    check("Reads all tickets and decides each one once",
          sorted(ids) == sorted(t["id"] for t in all_tickets),
          f"helpdesk has {len(all_tickets)} tickets; log has {len(ids)} decisions, "
          f"{len(set(ids))} different ids")
    bodies = [line.get("sent") or {} for line in final]
    check("Every decision has language, category, priority and reason",
          all(b.get("language") and b.get("category") and b.get("priority") and b.get("reason")
              for b in bodies), f"{len(bodies)} bodies checked")
    check("Values are allowed by api_reference.md",
          all(b.get("language") in ("nl", "en", "de")
              and b.get("category") in ("bug", "billing", "feature_request", "question", "other")
              and b.get("priority") in ("P1", "P2", "P3", "P4") for b in bodies),
          "language nl/en/de, the five categories, P1 to P4")
    actions = {line["action"] for line in final}
    check("Each action is reply, handoff or close", actions <= {"reply", "handoff", "close"},
          f"actions used: {sorted(actions)}")
    reasons = [b.get("reason", "") for b in bodies]
    check("The reason is short (one or two sentences)",
          all(len(re.findall(r"[.!?](\s|$)", r)) <= 3 and len(r) <= 300 for r in reasons),
          f"longest reason {max(map(len, reasons))} characters")
    replies = [(line, line["sent"]) for line in final if line["action"] == "reply"]
    check("A reply has a draft; a handoff has a note",
          all(b.get("draft_reply") for _, b in replies)
          and all(line["sent"].get("handoff_note") for line in final if line["action"] == "handoff"),
          f"{len(replies)} replies, {sum(l['action'] == 'handoff' for l in final)} handoffs")
    closes = [line for line in final if line["action"] == "close"]
    check("Close only for spam or empty (the code saw it too)",
          all(line["guard"]["close_signal"] for line in closes),
          "; ".join(f"{l['ticket_id']}: {l['guard']['close_signal']}" for l in closes) or "no closes")
    check("Sending everything to a human is avoided",
          0 < len(replies) and sum(l["action"] == "handoff" for l in final) < len(final),
          f"{len(replies)} of {len(final)} answered directly")

    print("\n== 03 It must work in Dutch ==")
    wrong_language = [line["ticket_id"] for line, body in replies
                      if detect(body["draft_reply"])[1] >= 2 and detect(body["draft_reply"])[0] != body["language"]]
    check("Replies are in the customer's language", not wrong_language,
          f"each reply's draft language matches its language field; mismatches: {wrong_language or 'none'}")
    notes = [(l["ticket_id"], l["sent"]["handoff_note"]) for l in final if l["action"] == "handoff"]
    not_english = [i for i, n in notes if detect(n)[0] != "en" and detect(n)[1] >= 2]
    check("Handoff notes are in English", not not_english,
          f"{len(notes)} notes; not English: {not_english or 'none'}")
    check("Every reply has an English view to read it without Dutch",
          all(line.get("english_view") for line, _ in replies),
          f"{sum(bool(l.get('english_view')) for l, _ in replies)} of {len(replies)}")

    print("\n== Client rules (faq.md) ==")
    check("Replies only use the help centre (quote and fact checks passed)",
          all(all(line["checks"].get(k) for k in ("quotes", "facts", "promises", "english_view"))
              for line, _ in replies),
          f"all {len(replies)} replies passed the quote, fact, promise and English-view checks")
    must = [l for l in final if l["guard"]["tier1"]]
    check("Must-handoff topics found by code always went to a human",
          all(l["action"] == "handoff" for l in must),
          "; ".join(f"{l['ticket_id']}: {','.join(l['guard']['tier1'])} -> {l['action']}" for l in must))
    injected = [l for l in final if l["guard"]["injection"]]
    check("Tickets with injection text went to a human",
          all(l["action"] == "handoff" for l in injected),
          f"{len(injected)} found, all handed off" if injected else "none in this set (tested in eval/my_tickets.jsonl)")
    clean_log = redact(log_text) == log_text
    check("No card numbers or IBANs in the log", clean_log,
          "the redaction finds " + ("nothing" if clean_log else "a payment number") + " to hide in the log file")
    code_text = "\n".join(p.read_text() for p in (ROOT / "agent").glob("*.py"))
    faq_spans = set().union(*(spans(entry, 8) for entry in faq.values()))
    copied_faq = [s for s in faq_spans if s in " ".join(re.findall(r"\w+", code_text.lower()))]
    check("The help centre is loaded at run time, not typed into the code", not copied_faq,
          f"8-word runs of faq.md found in agent/*.py: {len(copied_faq)}")

    print("\n== 05 We read your code: no hand-typed answers ==")
    searched = [p for p in (ROOT / "agent").glob("*.*")] + [ROOT / "eval" / "run_eval.py"]
    searched_text = " ".join(re.findall(r"\w+", " ".join(p.read_text() for p in searched).lower()))
    ticket_spans = set()
    for item in all_tickets + examples:
        ticket_spans |= spans(f"{item.get('subject', '')} {item.get('body', '')}")
    copied = sorted(s for s in ticket_spans if s in searched_text)
    check("No ticket or example text in the agent code, prompts or word lists", not copied,
          f"6-word runs from {len(all_tickets)} tickets and {len(examples)} examples found: "
          f"{copied[:3] or 'none'}")
    id_hits = re.findall(r"\b(?:T\d{3}|E\d{2})\b", code_text + (ROOT / "agent" / "rules_phrases.json").read_text())
    check("No ticket or example ids in the code", not id_hits, f"found: {id_hits or 'none'}")

    print("\n== 03 Production code ==")
    git_text = "\n".join(p.read_text(errors="ignore") for p in tracked_files() if p.is_file())
    keys = re.findall(r"sk-ant-\w{6}|nrd_\w{6}", git_text)
    check("No keys in any tracked file", not keys, f"key shapes found: {len(keys)}")
    env_example = (ROOT / ".env.example").read_text()
    check(".env is ignored and .env.example has no values",
          ".env" in (ROOT / ".gitignore").read_text().split()
          and not re.search(r"^(?:HELPDESK_KEY|LLM_API_KEY|CANDIDATE_EMAIL)=.+", env_example, re.M),
          "keys and email come from the environment only")
    print("      503, 429, 409, crash, two agents: run  python -m scripts.reliability_test")

    print("\n== 04 Measure it and fix one thing ==")
    results_dir = ROOT / "eval" / "results"
    for name in ("before", "after"):
        path = results_dir / f"{name}.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        check(f"eval/results/{name}.json exists, labels by Nordavo",
              data.get("labelled_by") == "Nordavo" and data.get("total") == len(examples),
              f"action {data.get('counts', {}).get('action')}/{data.get('total')}, unsafe {data.get('unsafe')}")

    print("\n== 06 What to deliver ==")
    readme = (ROOT / "README.md").read_text()
    headings = re.findall(r"^## (.+)$", readme, re.M)
    wanted = ["How to run", "Decisions", "Reliability", "Eval and fix", "What I would improve"]
    check("README has the five headings", headings == wanted, f"found: {headings}")
    words = len(readme.split())
    check("README is at most one page", words <= 560, f"{words} words")
    check("README has the cost per 100 tickets with the calculation",
          "per 100 tickets" in readme.lower() and "per million" in readme.lower(), "found")
    real_logs = sorted((ROOT / "logs").glob("run-*.jsonl"))
    check("Real-run logs are in logs/", bool(real_logs), f"{len(real_logs)} real-run log file(s)",
          todo=not real_logs)
    check("Final run done (reset, then all 30 tickets once)",
          status.get("handled_tickets") == len(all_tickets) and bool(real_logs),
          f"helpdesk now: open {status.get('open_tickets')}, handled {status.get('handled_tickets')}",
          todo=status.get("handled_tickets") != len(all_tickets))
    dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=ROOT).stdout
    check("Everything is committed", not dirty.strip(),
          f"{len(dirty.splitlines())} uncommitted file(s)", todo=bool(dirty.strip()))

    print(f"\n{results.count('PASS')} PASS, {results.count('FAIL')} FAIL, {results.count('TODO')} TODO")
    return 1 if "FAIL" in results else 0


if __name__ == "__main__":
    sys.exit(main())
