# Roosterly support ticket agent

Reads open helpdesk tickets and posts one decision each: reply, handoff or close.

## How to run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in the helpdesk key, your email and an Anthropic key
pytest -q                       # 186 offline tests, no network
python -m agent.run --dry-run   # reads and decides, never posts
python -m agent.run             # real run, asks for "y"
python -m eval.run_eval --repeat 3
```

`python -m agent.reset` re-opens the practice tickets. It asks you to type `reset`.

## Decisions

1. **The model fills a form. Code decides and posts.** The model has no tools, so text
   inside a ticket cannot make it act. Must-handoff topics are caught by a word list in
   code or by the model. Either one is enough.
2. **Dutch is handled directly, and checked in English.** Translating first costs a
   call per ticket and loses menu names. The model drafts in the customer's language.
   Code then checks the draft against the help centre: every link, number, price and menu
   name must come from the quoted entry. A second call translates the draft to English,
   and code checks that view for promises and unknown names. The view is in the log, so I
   can read what was sent.
3. **Confidence is not the safety mechanism.** A model's own confidence is often too
   high. The main reason to hand off is "the help centre does not cover this".

AI tools: built with Claude Code. It wrote code and tests from my requirements and ran an
independent audit and a red-team pass. I chose the design, the model and the fix.

Assumptions: a mixed ticket (how-to plus refund) goes to a human as a whole. A plan change
request is a contract change. Unsure means below 0.75 confidence or any failed check.
Unsupported languages are handed off with `en`. More in `docs/decisions.md`.

## Reliability

- **503 and timeouts:** up to 5 tries with growing random waits. Before retrying a POST,
  the agent reads the ticket to see if it was already saved.
- **429:** waits `Retry-After`. A limiter keeps calls 0.30 s apart.
- **409:** never retried. Counted in the summary.
- **Crash:** the helpdesk is the only state. A re-run reads the open tickets and continues.
- **Security:** keys only in `.env`. IBANs and card numbers are hidden before the model,
  the logs and every outgoing text. Ticket text sits in a random tag as data. Injection
  attempts are handed off. I do not claim injection is solved, only that the model cannot act.

## Eval and fix

10 Nordavo examples, graded by code. The model never writes labels.

| | Action | Category | Priority | Unsafe |
|---|---|---|---|---|
| Before | 7/10 | 9/10 | 8/10 | 0 |
| After (3 runs) | 9, 9, 9 | 10/10 | 10/10 | 0 |

**The fix:** answerable tickets were handed off. The prompt now says a question is covered
when the help centre states a requirement that explains the problem. A reply may repeat the
customer's own numbers, never as a price. Spam with no listed word closes only when a
second, narrow model call agrees. 10 examples cannot prove much: 9/10 means 60% to 98%.
My 16 own tickets (`eval/my_tickets.jsonl`) score 16/16.

**Cost:** 30 ticket passes used 113,835 input and 31,149 output tokens. At $0.10 and
$0.50 per million that is $0.027, or EUR 0.025. Per 100 tickets: EUR 0.08.

## What I would improve

Next: cache the fixed prompt, and trim `decide.py`. The same ticket can get a different
answer on two runs, so I would run borderline tickets twice and hand off on disagreement.

One rule I would change: a handed-off customer hears nothing. I would send a short
"we received your message" note with every handoff.
