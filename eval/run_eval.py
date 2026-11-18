"""Eval on Nordavo's labelled examples. Graded by code against their labels.

  python -m eval.run_eval               print the result
  python -m eval.run_eval --save before also write eval/results/before.json

The model being tested never writes a label, and the examples are never part of
the prompt. Nothing is posted: examples are read-only.
"""

import argparse
import json
import sys

from agent import config, llm
from agent.helpdesk import Helpdesk
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


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", help="name of the result file, for example before")
    args = parser.parse_args(argv)

    settings = config.load()
    helpdesk = Helpdesk(settings.helpdesk_url, settings.helpdesk_key, settings.email, dry_run=True)
    examples = helpdesk.examples()
    faq = llm.load_faq()

    decisions, tokens_in, tokens_out = [], 0, 0
    for example in examples:
        try:
            decision, _, _, used_in, used_out = triage(example, settings, faq)
            tokens_in, tokens_out = tokens_in + used_in, tokens_out + used_out
        except llm.ProviderDown as error:
            print(f"{example['id']}: model unavailable ({error})", file=sys.stderr)
            decision = None
        decisions.append(decision)

    rows, counts, unsafe, missed_reply = grade(examples, decisions)
    total = len(examples)
    model = settings.model or "none (rules only)"
    print(f"Eval on {total} examples  model {model}  commit {commit_hash()}")
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
    cost = config.cost_eur(settings.model, tokens_in, tokens_out)
    print(f"Tokens in {tokens_in}, out {tokens_out}. Cost EUR {cost:.4f}, "
          f"per 100 tickets EUR {cost / total * 100:.4f}")
    print("Note: 10 examples cannot prove a small improvement. Read the table, not just the counts.")

    if args.save:
        result = {
            "saved_at": utc_now(), "commit": commit_hash(), "model": model,
            "labelled_by": "Nordavo", "threshold": settings.threshold,
            "total": total, "counts": counts,
            "unsafe": unsafe, "answerable_but_handed_off": missed_reply,
            "tokens": {"in": tokens_in, "out": tokens_out}, "rows": rows,
        }
        path = config.ROOT / "eval" / "results" / f"{args.save}.json"
        path.write_text(json.dumps(clean(result), indent=2) + "\n")
        print(f"Saved {path}")


if __name__ == "__main__":
    main()
