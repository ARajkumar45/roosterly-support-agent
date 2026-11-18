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
| `language` for a ticket not in nl, en or de | Send `en`, handoff; the note says it is not in Dutch, English or German | assumption |
| `language` for an empty or spam ticket | Send `nl` | assumption |
| Thank-you-only or one-word ticket | Handoff, P4 (close is only for spam and empty) | assumption |
| GDPR request priority | P2 | assumption, check examples |
| Category when the model step failed | Refund, cancel, contract = billing. GDPR, legal, churn = other. Security = other | assumption, check examples |
| Billing how-to | P4 | assumption, check examples |
| Sign-off in replies | A greeting, a closing line, then "Team Roosterly" | assumption |
| Form of address | The one the customer used (je/u, du/Sie); u or Sie when the ticket shows neither | assumption |
| A ticket that tries to instruct the agent | Handoff, even when the real question is easy | choice: safe over useful |
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
- More must-handoff wordings in the code list. (The second review later moved a few words
  that also describe an employee to the hint list; see below.)
- "It does not work" alone no longer counts as "already tried". The model's field decides.

**Hardening found by an independent red-team pass**, each with a test in `tests/test_fixes.py`:
- Payment details are hidden also when split by invisible characters, mixed separators,
  quoted line breaks or table cells.
- Common promise wordings are caught ("a colleague will", "within 2 weeks", "you will
  hear"), and names in single quotes must come from the help centre.
- A reply in a different language from the ticket goes to a human.
- One broken ticket is logged and left open; it never stops the run.
- A crash between "saved" and "logged" is repaired on restart: the missing log line is written.
- The model address is fixed in code, so a shell variable cannot send the key elsewhere.

## Second review (after the final run)

I asked for a check that does not trust the agent's own checks. Five reviewers worked
separately: a blind answer key for the 30 tickets (written without seeing our decisions),
a ticket-by-ticket comparison with it, a code review against every sentence of the brief,
a fresh-clone test, and a literature search.

**What it found in the final run.** Safety: no problems (8 of 8 must-human tickets handed
off, both injection attempts ignored, no payment details, every reply fact in the help
centre). Against the blind key, 26 of 30 tickets match on all four fields. Two real misses:
an English ticket about the German app was labelled `de`, and an answerable ticket was
handed off because the fact check refused a correct price from a second quoted entry.

**What it found in the code**, each fixed with a test that failed before:
- Tests read the real lock file `.final_run_done`, so 12 failed after the final run.
  `run()` and `reset()` now have no default paths.
- A handoff now carries the language the code counts in the text when the count is sure.
- Notes lost names that look like web addresses ("Loket.nl"). Only real links are removed now.
- Reasons read badly ("refund always go to a human") and never said why the priority.
  Every rule reason is now two sentences: why a human, then why this category and priority.
- A code-rule handoff threw away the model's note; it is kept now, within 60 words.
- Facts may come from every help-centre entry the model quoted word for word.
- The code word list caught "I am not logged in" as a security incident and "an employee
  resigned" as a cancellation. Both are fixed; a cancel word now needs the subscription or
  account in the same sentence, or a writer who cancels.
- The injection list missed "ignore all previous instructions" and similar forms.
- Card numbers in unusual groups and an IBAN glued to a word were not hidden.
- A long Retry-After was cut to 30 s; it is now respected up to 300 s.
- The rule checker had checks that could not fail. It now marks them SAME (the agent agrees
  with itself) or NONE (nothing to check), and checks payment details with its own patterns.
- One help-centre address was typed into the code; it is gone, and a test keeps it out.

**Measured after the fixes.** Nordavo examples: 10, 9, 9 over three runs, 0 unsafe. Four dry
runs on the 30 practice tickets, scored against the blind key: 24, 26, 24, 25 of 30 on all four
fields (the final run: 26). Not better yet: a slip of the model or of the back-translation still
cost good replies, a different one each run.

**Production fixes for those slips** (a slip must not cost a good reply; no check got weaker):
- One repair try: when only the fact or quote check stops a draft, the model gets the exact
  problem ("menu name not in the help centre: Instellungen") and writes the form once more.
  The new form goes through every rule and check again. A promise, a payment detail or a
  wrong language is never repaired.
- The back-translation is done once more when the only problem looks like a translation slip
  (a name translated, a number written differently). Never when it shows a promise.
- A back-translation that is not in English is not trusted (it once came back in Dutch, where
  the English promise check finds nothing). An English draft is checked as it is.
- The customer's plan from the helpdesk counts as a known name, so "Basis" is not "unknown".
- The translation compare uses digits and links; spelled numbers are checked by the fact check.

**Measured after the production fixes.** Nordavo examples: 10, 9, 10, 0 unsafe. Own 46
tickets: 45/46, 0 unsafe. Three dry runs on the 30: 27, 26, 27 of 30 on all four fields,
always 8/8 must-human tickets to a human, 30/30 languages, every reason at most two
sentences. The repair try saved a correct reply twice. What is left: two priority hard calls
(P2 or P3, P3 or P4), and the injection ticket that goes to a human by choice.

## Final run and the live session

After the final run, create the file `.final_run_done` (`touch .final_run_done`). From then
on a real run and a reset are refused, so the scored result cannot be overwritten by
accident. Delete the file before the live session, where the agent runs on new tickets.

## Known limits

- The must-handoff word list will miss new wordings. The model is the second net.
- Marking ticket text as data lowers injection success but does not remove it (Hines et al.
  2024, "Spotlighting"). The safety comes from the code gates, not from the prompt.
- A made-up fact with no number, link or name (for example "free on Basis") cannot be
  caught by code. Only the model's own care, and the help-centre quote it must give, guard it.
- The model does not accept temperature 0, so answers can differ between runs. The eval
  runs several times (`--repeat`) and reports which tickets changed.
- The English check can still stop a good reply after its second try. That costs a handoff, never a wrong reply.
- Back-translation can hide errors: words left untranslated come back unchanged, and "u"
  and "je" both become "you" (Zhuo et al. 2023). It checks facts, not tone.
- The model's own confidence is often too high (Xiong et al. 2024). The 0.75 cut-off decided
  no ticket in the final run. It is now logged for every ticket so it can be tested.
- Two agents started at the same time can each send a decision for the same ticket; the
  helpdesk refuses the second (409). Run one at a time.
- My own 46 test tickets were labelled with Claude Code and reviewed by me, not by a
  Dutch speaker.
- The agent is about 1,870 lines, more than I planned. The decision step is a list of small named rules (`RULES` in `agent/decide.py`), so each rule can be read and changed on its own.

## Questions to Nordavo

None sent. The pack files are the source of truth. Every open point uses the default in
the table above and is listed in the README as an assumption.
