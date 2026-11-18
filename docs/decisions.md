# Decisions and assumptions

Items marked (assumption) are my choices, not Nordavo rules.

## Design

- Fixed workflow, not a free agent. The model fills a schema. Code decides and posts.
- Two safety layers for must-handoff topics: a code guard OR the model. Either one forces a handoff.
- The helpdesk is the source of truth for what is handled. No local journal.

## Assumptions (open until Nordavo answers or the examples settle them)

| Topic | Default | Status |
|---|---|---|
| How-to plus refund in one ticket | Whole ticket goes to a human | assumption |
| "How do I upgrade" | Reply from F05 | assumption |
| Request that we upgrade, downgrade, pause or end the plan | Handoff, billing, P2 | assumption |
| Many staff or locations blocked now | Handoff, bug, P1 | assumption |
| One customer asks if there is an outage | Reply from F11, no claim about an outage | assumption |
| `language` for a ticket not in nl, en or de | Send `en`, handoff, name the real language in the note | assumption |
| `language` for an empty or spam ticket | Send `nl` | assumption |
| Thank-you-only or one-word ticket | Handoff, P4 (close is only for spam and empty) | assumption |
| GDPR request priority | P2 | assumption, check examples |
| Category when the model step failed | Refund, cancel, contract = billing. GDPR, legal, churn = other. Security = other | assumption, check examples |
| Billing how-to | P4 | assumption, check examples |
| Sign-off in replies | "Team Roosterly" | assumption |
| Confidence threshold | 0.75 | assumption; no example was decided by it |
| Request rate | One request per 0.30 s (the API allows 5 a second) | choice |
| Nordavo sees every request (409s, rate) | Treated as true | assumption, not stated in the pack |

## Differences found in the 10 examples

Found by running the eval and reading the misses. These are label styles, turned into
general rules in the prompt. No example text is in the code or the prompt.

- A how-to about invoices, prices or plans is labelled `billing`, not `question`.
- A problem that the help centre explains (a requirement, a self-service step) gets a reply,
  and is P3 for a problem or P4 for a how-to. It is not P2.
- A feature request gets a reply that points to the help-centre entry on ideas.
- Unsolicited advertising is closed as spam.

## The one fix, and the other changes

**The fix.** The agent handed off tickets that the help centre answers. The model's rules
now say a question is covered when the help centre states a requirement or limit that
explains the problem, or says how customers do it themselves. One supporting change was
needed for it to reach the customer: a reply may repeat a number the customer wrote (a
version number), but a number next to money or time must come from the help centre.
Before: action 7/10. After: 9/10 in three runs.

**Other changes, made separately and measured with the same eval:**
- Close still needs two signals. When the code word list sees no spam, a second, narrow
  model call may confirm it (SPAM or CUSTOMER). It is skipped when any guard hint fired.
  Risk I accept: both calls use the same model, so they are not fully independent.
- A ticket that asks nothing (only thanks, a greeting) goes to a human.
- More must-handoff wordings in the code list. Nothing was removed: a false handoff is
  cheap, a missed one is not.
- "It does not work" alone no longer counts as "already tried". The model's field decides.

**Hardening found by an independent red-team pass**, each with a test in `tests/test_fixes.py`:
- Payment details are hidden also when split by invisible characters, mixed separators,
  quoted line breaks or table cells.
- Promises are caught in any form ("a colleague will", "within 2 weeks"), and names in
  single quotes must come from the help centre.
- A reply in a different language from the ticket goes to a human.
- One broken ticket is logged and left open; it never stops the run.
- A crash between "saved" and "logged" is repaired on restart: the missing log line is written.
- The model address is fixed in code, so a shell variable cannot send the key elsewhere.

## Final run and the live session

After the final run, create the file `.final_run_done` (`touch .final_run_done`). From then
on a real run and a reset are refused, so the scored result cannot be overwritten by
accident. Delete the file before the live session, where the agent runs on new tickets.

## Known limits

- The must-handoff word list will miss new wordings. The model is the second net.
- A made-up fact with no number, link or name (for example "free on Basis") cannot be
  caught by code. Only the model's own care, and the help-centre quote it must give, guard it.
- The model does not accept temperature 0, so answers can differ between runs. The eval
  runs several times (`--repeat`) and reports which tickets changed.
- The English check sometimes stops a good reply. That costs a handoff, never a wrong reply.
- My own 46 test tickets were labelled with Claude Code and reviewed by me, not by a
  Dutch speaker.
- The agent is about 1,700 lines, more than I planned. The decision step is a list of small named rules (`RULES` in `agent/decide.py`), so each rule can be read and changed on its own.

## Questions to Nordavo

None sent. The pack files are the source of truth. Every open point uses the default in
the table above and is listed in the README as an assumption.
