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

## The fix and what came with it

- Coverage rule in the prompt (see above). Before: action 7/10. After: 9/10 in three runs.
- A reply may repeat a number the customer wrote, but every amount of money or percentage
  must come from the help centre.
- Close still needs two signals. The second may be a separate, narrow model call that
  answers SPAM or CUSTOMER. It is skipped when any guard hint fired. Risk I accept: both
  calls come from the same model, so they are not fully independent.
- More must-handoff wordings were added to the code list. Nothing was removed from it:
  a false handoff is cheap, a missed one is not.
- "It does not work" alone no longer counts as "already tried". The model's field decides.
- The model address is fixed in code, so a shell variable cannot send the key elsewhere.
- The model does not accept temperature 0. Answers can differ between runs, so the eval
  can run several times (`--repeat`) and reports which tickets changed.

## Known limits

- The must-handoff word list will miss new wordings. The model is the second net.
- The English check sometimes stops a good reply (a translated name looks unknown).
  That costs a handoff, never a wrong reply.
- 10 examples and 16 own tickets are a small sample. My own labels were drafted with
  Claude Code and reviewed by me, not by a Dutch speaker.
- Code outside tests is about 1,700 lines, more than I planned.

## Questions to Nordavo

None sent. The pack files are the source of truth. Every open point uses the default in
the table above and is listed in the README as an assumption.
