# Roosterly support ticket agent

## How to run

```bash
# tested with Python 3.15
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # fill in the keys and your email
pytest -q                          # offline, no keys
python -m scripts.reliability_test # fake local helpdesk, no keys, ~2 min
python -m agent.run --dry-run      # decides, never posts
python -m agent.run                # real run, asks for "y"
python -m eval.run_eval --repeat 3 # about EUR 0.03
```

`python -m agent.reset` re-opens the practice tickets. After my final run, `.final_run_done`
blocks real runs and resets; delete it for new tickets.

## Decisions

1. **The model fills a form; code decides and posts.** The model has no tools, so ticket
   text cannot make it act. Must-handoff topics: a code word list or the model.
2. **Dutch is answered directly and checked in English.** Every link, price, time and menu
   name in a draft must come from a quoted help-centre entry. A second call translates it
   to English; code checks that for promises and unknown names. A wrong name gets one repair try.
3. **"Not covered" is the main reason to hand off, not confidence.** Model confidence
   is often too high.

AI tools: Claude Code wrote code and tests from my requirements and ran an audit, a
red-team pass and a blind review. I chose the design, model and fix.
Assumptions (`docs/decisions.md`): a mixed ticket goes to a human; a plan change request
is a contract change; unsure means confidence below 0.75 or any failed check.

## Reliability

- **503, timeouts:** 5 tries, growing random waits. Before retrying a POST, it reads the
  ticket to see if it was saved.
- **429:** waits `Retry-After`; calls are 0.30 s apart. **409:** never retried.
- **Crash:** the helpdesk is the only state; a re-run continues and writes any missed log
  line. `scripts/reliability_test.py` proves this over HTTP: each time 30 of 30 saved once.
- **Security:** keys only in `.env`. IBANs and card numbers are hidden before the model,
  logs and notes. Ticket text is wrapped as data; instructions inside it send it to a human.

## Eval and fix

10 Nordavo examples, graded by code; the model writes no labels.

| | Action | Category | Priority | Unsafe |
|---|---|---|---|---|
| Before | 7/10 | 9/10 | 8/10 | 0 |
| After, 3 runs | 10, 9, 9 | 10/10 | 10/10 | 0 |
| After review | 10, 9, 10 | 10/10 | 10/10 | 0 |

**The fix:** answerable tickets were handed off. A question now counts as covered when
the help centre states a requirement that explains the problem. 9/10 still means 60% to
98%. My 46 own edge-case tickets (`eval/my_tickets.jsonl`): 45/46, 0 unsafe.

**Final run** (after the review fixes): `logs/run-20261118T182501Z.jsonl`:
30 tickets, 10 replies, 18 handoffs, 2 closes, 0 errors. A blind answer key matches 26/30
on all four fields (the 4: hard calls and one injection).

**Cost:** 30 tickets used 128,936 input and 38,839 output tokens, repair tries included.
At $0.10 and $0.50 per million: $0.0323, EUR 0.030. **Per 100 tickets: EUR 0.10.**

## What I would improve

Next: ask the model twice on borderline tickets and hand off when the answers differ.
One rule I would change: a handed-off customer hears nothing; I would send a short
"we received your message" note.
