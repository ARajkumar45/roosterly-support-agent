# Design sources

Each source was opened and checked on 2026-11-17. The table says what the source really
supports, and where the idea lives in the code. Where the code goes further than the
source, that is said plainly.

| Idea in this repo | Where in the code | Source | What the source supports |
|---|---|---|---|
| Fixed workflow. The model fills a form, code acts | `agent/decide.py`, `agent/run.py` | Anthropic, "Building effective agents" (2024). https://www.anthropic.com/engineering/building-effective-agents | Workflows are predefined code paths. Use the simplest solution that works. |
| The model picks from fixed actions | `ModelOutput.action` in `agent/decide.py` | Beurer-Kellner et al. 2025, "Design Patterns for Securing LLM Agents against Prompt Injections". https://arxiv.org/abs/2506.08837 | Names the "action-selector" pattern: the model chooses from a fixed list, like a switch statement. |
| Ticket text never steers the program | `agent/decide.py` | Debenedetti et al. 2025, "Defeating Prompt Injections by Design" (CaMeL). https://arxiv.org/abs/2503.18813 | Control flow comes only from trusted input. This repo follows the principle. It does not build CaMeL's full system. |
| Ticket text is wrapped in a random tag and marked as data | `build_user_message` in `agent/llm.py` | Hines et al. 2024, "Defending Against Indirect Prompt Injection Attacks With Spotlighting". https://arxiv.org/abs/2403.14720 and OWASP LLM01:2025. https://genai.owasp.org/llmrisk/llm01-prompt-injection/ | Marking untrusted text lowers attack success a lot. It is a mitigation, not a guarantee. That is why the code guard also forces a handoff on injection markers. |
| A reply must quote the help centre | `reply_checks` in `agent/decide.py` | Anthropic docs, "Reduce hallucinations". https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations | Ask the model for word-for-word quotes first. The check in code that each quote, link, number and menu name exists in the FAQ is my own addition. |
| Retries wait longer each time, with a random part | `Helpdesk.request` in `agent/helpdesk.py` | Marc Brooker, AWS, "Exponential Backoff and Jitter" (2015). https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/ | Jitter cuts load and client work. "Full jitter" does the least work. The code uses full jitter. |
| A blind retry of a POST is unsafe | `Helpdesk.post_action` in `agent/helpdesk.py` | Brandur Leach, Stripe, "Designing robust and predictable APIs with idempotency" (2017). https://stripe.com/blog/idempotency | Explains why a retried POST can run twice, and solves it with an idempotency key. The helpdesk has no such key. Asking the helpdesk "is this ticket handled?" before every retry is my own workaround. |
| The tested model writes no labels | `eval/run_eval.py` | Zheng et al. 2023, "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena". https://arxiv.org/abs/2306.05685 | LLM judges show biases, one of them favouring their own output. So grading is done by code against Nordavo's labels. |
| The confidence threshold is chosen on labelled tickets | `CONFIDENCE_THRESHOLD` in `agent/config.py` | Geifman and El-Yaniv 2017, "Selective Classification for Deep Neural Networks". https://arxiv.org/abs/1705.08500 | Pick the reject threshold on labelled data to meet a risk level. The paper is about image classifiers. Using it for a model's stated confidence is my extrapolation, so the code checks stay the main gate. |
| A Dutch reply is translated back to English and compared | `english_view`, `view_matches` | Zhuo et al. 2022, "Rethinking Round-Trip Translation for Machine Translation Evaluation". https://arxiv.org/abs/2209.07351 | Round-trip translation is a useful automatic signal for modern translation. The paper does not call it proof. I use it only as a smoke test: numbers and links must match. It says nothing about tone. |

The README cites at most one of these.
