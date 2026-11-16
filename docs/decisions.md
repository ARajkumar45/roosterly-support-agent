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
| Confidence threshold | 0.75 to start | assumption, tune in Phase 6 |
| Request rate | One request per 0.30 s (the API allows 5 a second) | choice |
| Nordavo sees every request (409s, rate) | Treated as true | assumption, not stated in the pack |

## Differences found in the 10 examples

(filled in Phase 0 after reading them with Raj)

## Questions to Nordavo

None sent. The pack files are the source of truth. Every open point uses the default in
the table above and is listed in the README as an assumption.
