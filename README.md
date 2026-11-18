# Roosterly support ticket agent

## How to run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # helpdesk key, your email, an Anthropic key
pytest -q                          # offline tests
python -m scripts.reliability_test # failure scenarios against a local fake helpdesk
python -m agent.run --dry-run      # decides, never posts
python -m agent.run                # real run, asks for "y"
python -m eval.run_eval --repeat 3
```

`python -m agent.reset` re-opens the practice tickets after you type `reset`.

## Decisions

1. **The model fills a form; code decides and posts.** The model has no tools, so text in
   a ticket cannot make it act. Must-handoff topics are caught by a code word list or the model.
2. **Dutch is answered directly and checked in English.** Every link, number, price and
   menu name in a draft must come from the quoted help-centre entry. A second call
   translates the draft to English; code checks it for promises and unknown names, and it
   is logged so I can read what was sent.
3. **"Not covered" is the main reason to hand off, not confidence.** A model's own
   confidence is often too high.

AI tools: built with Claude Code, which wrote code and tests from my requirements and ran
an independent audit and red-team pass. I chose the design, model and fix.
Assumptions (more in `docs/decisions.md`): a mixed ticket goes to a human as a whole; a
plan change request is a contract change; unsure means confidence below 0.75 or any failed check.

## Reliability

- **503, timeouts:** 5 tries, growing random waits. Before retrying a POST, it reads the
  ticket to see if the first try was saved.
- **429:** waits `Retry-After`; calls are kept 0.30 s apart. **409:** never retried.
- **Crash:** the helpdesk is the only state; a re-run continues, and writes any log line
  a crash missed. `scripts/reliability_test.py` proves this over real HTTP: 1 in 4 calls
  failing, lost responses, a killed process, two agents at once. Each time 30 of 30 saved once.
- **Security:** keys only in `.env`. IBANs and card numbers are hidden before the model,
  logs and notes. Ticket text sits in a random tag as data. Injection is not solved; the model just cannot act.

## Eval and fix

10 Nordavo examples, graded by code; the model writes no labels.

| | Action | Category | Priority | Unsafe |
|---|---|---|---|---|
| Before | 7/10 | 9/10 | 8/10 | 0 |
| After, 3 runs | 10, 9, 9 | 10/10 | 10/10 | 0 |

**The fix:** answerable tickets were handed off. The prompt now counts a question as
covered when the help centre states a requirement that explains the problem. 9/10 still
means 60% to 98%. My 46 own edge-case tickets (`eval/my_tickets.jsonl`): 45/46, 0 unsafe.

**Final run:** `logs/run-20261118T131846Z.jsonl`, one line per ticket: 30 tickets, 10 replies,
18 handoffs, 2 closes, 0 errors. Rehearsal logs are in `logs/before_reset_*`.

**Cost:** 30 ticket passes used 119,407 input and 30,078 output tokens. At $0.10 and
$0.50 per million: $0.0270, EUR 0.025. **Per 100 tickets: EUR 0.08.**

## What I would improve

Next: run borderline tickets twice and hand off when the answers differ.
One rule I would change: a handed-off customer hears nothing. I would send a short
"we received your message" note with every handoff.
