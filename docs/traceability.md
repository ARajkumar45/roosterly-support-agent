# Requirements table

Rule: no phase is done until every row it touches has a real function and test name.
The Evidence column is filled by the auditor before the final run.

| ID | Requirement | Enforced in | Proof |
|---|---|---|---|
| A01 | One command runs the agent from a fresh clone | `agent/run.py` | fresh clone in a temp folder, README commands as written |
| A02 | Reads all open tickets: limit 10, follow next_cursor to null, ids collected first, deduplicated | `helpdesk.list_tickets` | `test_pagination_collects_all`, `test_repeating_cursor_stops` |
| A03 | Exactly one decision per ticket: reply, handoff or close | `decide.decide`, run loop | `test_one_post_per_ticket` |
| A04 | `language` posted is always nl, en or de, for all three actions | `helpdesk.build_body` | `test_other_language_handoff`, `test_empty_ticket_language` |
| A05 | `category` is bug, billing, feature_request, question or other | body models | `test_schema_rejects_unknown_category` |
| A06 | `priority` P1 to P4 by the brief; code floor only raises | system prompt, `rules.PRIORITY_FLOOR` | `test_floor_only_raises` |
| A07 | `reason`: English, one or two sentences, says why this category, priority and action, and matches the final action after any code override | `decide.handoff`, `decide.model_reason` | `test_reason_matches_final_action` |
| A08 | reply posts draft_reply; handoff posts handoff_note; close posts neither; no other fields; validated before sending | `helpdesk.build_body` | `test_body_fields_per_action` |
| A09 | Reply is in the language the customer wrote in | `decide.reply_checks` | `test_wrong_language_draft_hands_off` |
| A10 | Handoff note: English, at most 60 words, actionable in under a minute | prompt, `decide.build_note` | `test_fallback_note_fields`, `test_non_english_note_replaced` |
| A11 | Close only for spam or empty, only when the code signal and the model agree | `rules.close_signal`, `decide` | `test_close_needs_both`, `test_subject_only_not_empty` |
| A12 | Replies use only faq.md, loaded at run time | `llm.load_faq`, `decide.reply_checks` | `test_quote_must_be_in_faq` |
| A13 | Every URL, number, price, menu path and quoted label in a draft exists in the cited FAQ entry | `decide.reply_checks` | `test_invented_url_hands_off`, `test_price_normalised` |
| A14 | No promises and no claims of actions taken | `rules_phrases.json` banned_in_reply, `decide.reply_checks` | `test_promise_hands_off` |
| A15 | A ticket the FAQ does not fully cover goes to a human | `decide` | `test_partly_covered_hands_off` |
| A16 | Customer already tried the FAQ steps: handoff | `decide` | `test_already_tried_hands_off` |
| A17 | Unsure means handoff: confidence under threshold or a reply check fails (no regenerate, see README left out) | `decide` | `test_low_confidence_hands_off` |
| A18 | Not everything is handed off; handoff rate by reason is reported | run summary, eval | summary line |
| A19 | Plan comes from `customer_plan`, never from ticket text; missing or unknown is fine | `llm.build_user_message` | `test_unknown_plan_ok` |
| A20 | No ticket ids, labels or answers in code; nothing assumes 30 tickets or ids starting with T | whole repo | `grep -rnE "\b[TE][0-9]{2,3}\b" agent/` finds nothing |
| S01 | Refunds and disputed charges always go to a human | `rules` tier 1, model | `test_tier1_refund` |
| S02 | Cancellations and contract changes always go to a human | same | `test_tier1_cancel` |
| S03 | Personal data requests (GDPR, AVG) always go to a human | same | `test_tier1_gdpr` |
| S04 | Possible security incidents always go to a human, P1 | same, floor | `test_tier1_security_p1` |
| S05 | Legal threats always go to a human, P1 | same, floor | `test_tier1_legal_p1` |
| S06 | Customers threatening to leave always go to a human | same | `test_tier1_churn` |
| S07 | A must-handoff topic anywhere in the ticket wins, even if the FAQ covers another part | `decide` | `test_howto_plus_refund_hands_off` |
| S08 | Weak words alone never force a handoff | `rules` tier 2 | `test_negative_phrases_not_forced` |
| S09 | No card number or IBAN in a reply | `rules.redact`, `decide` | `test_draft_with_iban_hands_off` |
| S10 | No card number or IBAN in any log line | `logs.write_line` | `test_log_line_redacted` |
| S11 | No payment details in LLM input, reason, note, eval results or error text | `redact` at each exit | `test_llm_input_redacted`, `test_error_text_redacted` |
| S12 | Ticket text is data: tagged, look-alike tags removed, model only fills a schema, code performs the action | `llm`, `decide` | `test_tag_lookalike_removed` |
| S13 | Injection markers force a handoff; reason and note never repeat the injected text | `rules.guard` | `test_injection_without_refund_words` |
| S14 | Model refusal, cut-off or invalid output: rule-based handoff; provider down: ticket left open, never a guess | `llm.fill_form`, `decide.fallback` | `test_invalid_output_falls_back`, `test_provider_down_leaves_open` |
| S15 | Keys only from environment; `.env` never committed; `.env.example` has names only; headers never logged | `config.py`, `.gitignore` | history search for the real key prefix prints 0 |
| S16 | No POST in dry-run, in tests, or for an id that did not come from GET /tickets in this run | `helpdesk.post_action` | `test_dry_run_never_posts`, `test_example_id_refused` |
| S17 | Reset is called in one file, asks for typed confirmation, refuses outside practice mode | `agent/reset.py` | `grep -rn "practice/reset" agent/` shows one file |
| R01 | 503 and other 5xx: retry with exponential backoff and jitter, 5 tries total | `helpdesk.request` | `test_503_then_success`, `test_503_gives_up_after_5` |
| R02 | 429: wait Retry-After, fallback wait when missing | `helpdesk.request` | `test_429_waits_retry_after`, `test_429_no_header` |
| R03 | 409: never retried, logged, counted | `helpdesk.post_action` | `test_409_not_retried` |
| R04 | 401 or missing_email stops the run; 404, 422, other 400 leave that ticket open and continue | `helpdesk`, `run` | `test_401_stops_run`, `test_422_leaves_open` |
| R05 | Never more than 4 requests in any second, retries included | `helpdesk.Limiter` | `test_20_calls_max_4_per_second` |
| R06 | Before any retry of an action, GET the ticket; if handled, stop and count as success | `helpdesk.post_action` | `test_timeout_after_save_no_second_post` |
| R07 | Retries exhausted: ticket left open, run continues, exit code 1 | `run.run` | `test_failed_ticket_exit_1` |
| R08 | After a crash a re-run continues and no ticket gets two answers; the helpdesk is the source of truth | `run.run` | `test_crash_then_rerun` |
| R09 | Both headers on every request; preflight prints mode and counts; same email every run | `helpdesk`, `run.run` | `test_headers_on_every_request` |
| L01 | One JSON log line per ticket, written when the ticket reaches its final state | `logs.write_line` | `test_one_post_and_one_line_per_ticket` |
| L02 | Logs hold no ticket body, are redacted, and real-run logs are committed | log schema | `git ls-files logs/` |
| M01 | Eval on the 10 examples, graded by code against Nordavo's labels | `eval/run_eval.py` | prints 4 counts and a per-example table |
| M02 | The tested model writes no labels; examples are not in the prompt | `llm.system_prompt`, `labelled_by` field | `test_prompt_has_no_example_text` |
| M03 | One real failure found, fixed, before and after results saved | `eval/results/` | both files exist with commit hash and model id |
| M04 | The fix is a general rule with a unit test on a new ticket, checked on the practice set | fix commit | named test, action diff printed |
| M05 | Cost per 100 tickets with the calculation, price source and date | `config.PRICES`, eval | README line equals eval output |
| M06 | Whole assignment well under 1 euro | per-run cap, spend total | summary line |
| M07 | A Dutch reply can be checked without reading Dutch | quote and fact checks, `english_view` | `test_english_view_present_for_reply` |
| M08 | Safety line (must-handoff examples not handed off, target 0) reported | eval | eval output |
| D01 | README at most one page, exactly five headings in the brief's order | `README.md` | `grep -c '^## ' README.md` prints 5, `wc -w` at most 500 |
| D02 | Decisions: three choices, Dutch handling and its cost, how a Dutch reply is checked, how AI tools were used, assumptions | README | reviewer read |
| D03 | Reliability: 503, 429, 409, crash, security | README | reviewer read |
| D04 | Eval and fix: result, fix, result after, cost per 100 with calculation | README | numbers equal `eval/results` |
| D05 | What I would improve: next step, one change to the brief or client rules, what was left out | README | reviewer read |
| D06 | Repo has code, eval, logs, `.env.example`; commits made while working | git | `git log --oneline` |
| D07 | Final run: one reset, one run, zero open after, zero 409, zero 422 | `agent.reset`, `agent.run` | summary and GET /status |
| D08 | Repo public, or private with read access for `nordavo-hiring` | GitHub | private browser window |
| D09 | Reply to the assignment email, subject "Take-home: <full name>", one line "Repo: <link>", no attachments, no video, before the deadline | Raj | sent mail |
| D10 | After the email: no commits, no real runs, no reset | `.final_run_done` marker | marker exists |
| D11 | Raj can explain and change every file | check questions | `docs/checks.md` |
