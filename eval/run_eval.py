"""Eval on Nordavo's labelled examples. Graded by code against their labels.

  python -m eval.run_eval               print the result
  python -m eval.run_eval --save before also write eval/results/before.json
  python -m eval.run_eval --repeat 3    run three times and show how steady the answers are
  python -m eval.run_eval --file eval/my_tickets.jsonl   my own test tickets

The model being tested never writes a label, and the examples are never part of
the prompt. Nothing is posted: examples are read-only.
"""

import argparse
import json
import sys

from agent import config, llm
from agent.helpdesk import Helpdesk, build_body
from agent.logs import clean
from agent.run import commit_hash, triage, utc_now

FIELDS = ["language", "category", "priority", "action"]


def wilson(hits, total, z=1.96):
    """95% Wilson interval for hits out of total. Honest about a small sample."""
    if not total:
        return 0.0, 0.0
    share = hits / total
    middle = share + z * z / (2 * total)
    spread = z * ((share * (1 - share) + z * z / (4 * total)) / total) ** 0.5
    bottom = 1 + z * z / total
    return (middle - spread) / bottom, (middle + spread) / bottom


def grade(examples, decisions):
    """Compare our decisions with the labels. Returns (rows, counts, safety lists)."""
    rows, counts = [], dict.fromkeys(FIELDS, 0)
    unsafe, missed_reply = [], []
    for example, decision in zip(examples, decisions):
        label = example["label"]
        ours = {name: getattr(decision, name, None) for name in FIELDS} if decision else {}
        if hasattr(decision, "fields"):
            # Grade the language that would really be posted ("other" becomes en or nl there).
            ours["language"] = build_body(decision.action, decision.fields())["language"]
        row = {"id": example["id"], "handoff_reason": getattr(decision, "handoff_reason", None)}
        for name in FIELDS:
            match = ours.get(name) == label[name]
            counts[name] += match
            row[name] = {"label": label[name], "ours": ours.get(name), "match": match}
        if label["action"] == "handoff" and ours.get("action") != "handoff":
            unsafe.append(example["id"])  # the label says human, we did not hand off
        if label["action"] == "reply" and ours.get("action") == "handoff":
            missed_reply.append(example["id"])  # answerable, but we handed off
        rows.append(row)
    return rows, counts, unsafe, missed_reply


def load_file(path):
    """Tickets with labels from a .jsonl file. Each line: id, subject, body, label, labelled_by."""
    with open(path) as file:
        return [json.loads(line) for line in file if line.strip()]


def run_once(examples, settings, faq):
    """One pass over the examples. Returns (decisions, tokens in, tokens out)."""
    decisions, tokens_in, tokens_out = [], 0, 0
    for example in examples:
        try:
            decision, _, _, used_in, used_out = triage(example, settings, faq)
            tokens_in, tokens_out = tokens_in + used_in, tokens_out + used_out
        except llm.ProviderDown as error:
            print(f"{example['id']}: model unavailable ({error})", file=sys.stderr)
            decision = None
        decisions.append(decision)
    return decisions, tokens_in, tokens_out


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", help="name of the result file, for example before")
    parser.add_argument("--file", help="labelled tickets in a .jsonl file (default: Nordavo's examples)")
    parser.add_argument("--repeat", type=int, default=1, help="run this many times and show how steady it is")
    args = parser.parse_args(argv)

    settings = config.load()
    if args.file:
        examples = load_file(args.file)
        labelled_by = sorted({e.get("labelled_by", "unknown") for e in examples})
    else:
        helpdesk = Helpdesk(settings.helpdesk_url, settings.helpdesk_key, settings.email, dry_run=True)
        examples, labelled_by = helpdesk.examples(), ["Nordavo"]
    faq = llm.load_faq()

    # The same model can answer differently on the same ticket, so one run is not the whole story.
    runs, tokens_in, tokens_out = [], 0, 0
    for _ in range(max(1, args.repeat)):
        decisions, used_in, used_out = run_once(examples, settings, faq)
        runs.append(grade(examples, decisions))
        tokens_in, tokens_out = tokens_in + used_in, tokens_out + used_out

    rows, counts, unsafe, missed_reply = runs[0]
    total = len(examples)
    model = settings.model or "none (rules only)"
    print(f"Eval on {total} examples  labels by {', '.join(labelled_by)}  model {model}  commit {commit_hash()}")
    print(f"{'id':6}" + "".join(f"{name:28}" for name in FIELDS))
    for row in rows:
        cells = [
            f"{'ok ' if row[n]['match'] else 'NO '} {row[n]['ours']} (label {row[n]['label']})"
            for n in FIELDS
        ]
        print(f"{row['id']:6}" + "".join(f"{cell:28}" for cell in cells))
    print("Counts: " + "  ".join(f"{name} {counts[name]}/{total}" for name in FIELDS))
    low, high = wilson(counts["action"], total)
    print(f"Action {counts['action']}/{total}: the true rate is likely between "
          f"{low:.0%} and {high:.0%} (95% Wilson interval).")
    print(f"Safety: label says handoff but we did not: {len(unsafe)} {unsafe} (target 0)")
    print(f"Label says reply but we handed off: {len(missed_reply)} {missed_reply}")

    steady = None
    if len(runs) > 1:
        action_scores = [run[1]["action"] for run in runs]
        unsafe_any = sorted({ticket_id for run in runs for ticket_id in run[2]})
        changed = [
            row["id"] for index, row in enumerate(rows)
            if len({run[0][index]["action"]["ours"] for run in runs}) > 1
        ]
        steady = {"runs": len(runs), "action_scores": action_scores,
                  "unsafe_in_any_run": unsafe_any, "action_changed_between_runs": changed}
        print(f"Over {len(runs)} runs: action scores {action_scores}, unsafe in any run {unsafe_any}, "
              f"action changed between runs on {changed or 'no ticket'}")

    cost = config.cost_eur(settings.model, tokens_in, tokens_out)
    passes = total * len(runs)
    print(f"Tokens in {tokens_in}, out {tokens_out}. Cost EUR {cost:.4f}, "
          f"per 100 tickets EUR {cost / passes * 100:.4f}")
    print(f"Note: {total} examples cannot prove a small improvement. Read the table, not just the counts.")

    if args.save:
        result = {
            "saved_at": utc_now(), "commit": commit_hash(), "model": model,
            "labelled_by": ", ".join(labelled_by), "threshold": settings.threshold,
            "total": total, "counts": counts,
            "unsafe": unsafe, "answerable_but_handed_off": missed_reply,
            "steadiness": steady,
            "tokens": {"in": tokens_in, "out": tokens_out}, "rows": rows,
        }
        path = config.ROOT / "eval" / "results" / f"{args.save}.json"
        path.write_text(json.dumps(clean(result), indent=2) + "\n")
        print(f"Saved {path}")


if __name__ == "__main__":
    main()
