"""The decision. The model fills in a form (ModelOutput). This code reads the
form and the guard result and picks the final action: reply, handoff or close.

The rules are small functions in RULES, checked in order. The first one that
returns a decision wins:

  injection            text tries to instruct the agent        -> handoff
  must_handoff_topic   refund, cancel, gdpr, security, ...     -> handoff
  too_long             the model could not read all of it      -> handoff
  outage               many users blocked right now            -> handoff, bug, P1
  plan_change          customer asks us to change the plan     -> handoff, billing, P2
  close                code AND model say spam or empty        -> close (one only: handoff)
  not_covered          the help centre does not answer it all  -> handoff
  already_tried        the customer did the steps already      -> handoff
  language             not nl, en or de, or a mismatch         -> handoff
  model_chose_handoff  the model itself said handoff           -> handoff
  checked_reply        all reply checks pass and sure enough   -> reply (else handoff)

After the rules: a handoff or close carries the language the code counts in the text
when the count is sure, because the model can mistake a language the customer mentions
for the language they wrote in.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from agent.rules import (
    FALLBACK_CATEGORY,
    PHRASES,
    apply_floor,
    found,
    has_payment_details,
    redact,
    strip_html,
)

# ---- the form the model fills in ----


class SubQuestion(BaseModel):
    text: str  # English
    covered: bool  # does the FAQ fully answer it?
    faq_id: str | None


class Quote(BaseModel):
    faq_id: str
    quote: str  # exact words from that FAQ entry


class ModelOutput(BaseModel):
    language: Literal["nl", "en", "de", "other"]
    category: Literal["bug", "billing", "feature_request", "question", "other"]
    priority: Literal["P1", "P2", "P3", "P4"]
    action: Literal["reply", "handoff", "close"]
    must_handoff_topic: Literal[
        "none", "refund", "cancel", "gdpr", "security", "legal", "churn"
    ]
    many_users_blocked: bool
    asks_plan_change: bool
    already_tried: bool
    summary: str  # English, what the customer wants
    sub_questions: list[SubQuestion]
    faq_quotes: list[Quote]
    draft_reply: str | None
    handoff_note: str  # English
    reason: str
    confidence: float = Field(ge=0, le=1)


@dataclass
class Decision:
    action: str
    language: str
    category: str
    priority: str
    reason: str
    draft_reply: str | None = None
    handoff_note: str | None = None
    confidence: float | None = None
    handoff_reason: str | None = None  # short key for the log and the summary
    model_confidence: float | None = None  # for the log only, never sent
    rejected_draft: str | None = None  # a draft a check stopped: for the log only, never sent
    repair_hint: str | None = None  # what to fix, when only the fact or quote check failed
    repaired: bool = False  # the reply came from the one repair try
    faq_ids: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def fields(self):
        """The fields that go into the request body."""
        fields = {
            "language": self.language,
            "category": self.category,
            "priority": self.priority,
            "reason": self.reason[:300],
            "draft_reply": self.draft_reply,
            "handoff_note": self.handoff_note,
            "confidence": self.confidence,
        }
        return {name: value for name, value in fields.items() if value is not None}


# ---- small text tools ----


def detect(text):
    """Guess nl, en or de by counting common small words.

    Returns (language, lead). Lead is how many more hits the winner has than
    the runner-up. A small lead means: not sure.
    """
    words = re.findall(r"[a-zà-ÿ]+", (text or "").lower())
    counts = {
        lang: sum(word in common for word in words)
        for lang, common in PHRASES["stopwords"].items()
    }
    ranked = sorted(counts, key=counts.get, reverse=True)
    return ranked[0], counts[ranked[0]] - counts[ranked[1]]


def squash(text):
    """Lower case, one kind of arrow, single spaces. For comparing texts."""
    text = (text or "").lower().replace("->", "→").replace(">", "→")
    return re.sub(r"\s+", " ", text).strip()


URL = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s),]*)?", re.IGNORECASE)
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
QUOTED = re.compile(r'["“„]([^"“”„]{2,60})["”]')
ARROW_STEP = re.compile(r"(\w+)\s*→\s*(\w+)")
LABEL_AFTER = re.compile(
    r"(?i:ga naar|klik op|tik op|onder|go to|click on|click|tap|unter)\s+"
    r"([A-Z][\w-]+(?:\s+[A-Z][\w-]+)*)"
)


def urls(text):
    """Links in the text, without a trailing full stop, slash or other punctuation."""
    return {u.lower().rstrip(".,;:!?\"'>/") for u in URL.findall(text or "")}


def numbers(text, words=True):
    """Numbers as values, so '€4', '€ 4' and '€4,00' are all 4.0.

    Number words count too ("vier weken" is 4), so a number cannot hide as a word.
    words=False counts digits only. A "1." at the start of a line is a list marker, not a fact.
    """
    text = URL.sub(" ", text or "")
    text = re.sub(r"(?m)^\s*\d+[.)]\s", " ", text)
    text = re.sub(r"(?<=\d)\.(?=\d{3}\b)", "", text)  # 1.000 is one thousand
    found_words = re.findall(r"[a-zà-ÿ]+", text.lower()) if words else []
    spelled = {float(PHRASES["number_words"][w]) for w in found_words if w in PHRASES["number_words"]}
    return {float(n.replace(",", ".")) for n in NUMBER.findall(text)} | spelled


def entry_body(entry):
    """A help-centre entry without its heading line, so 'F05' is not read as the number 5."""
    return entry.split("\n", 1)[1] if "\n" in entry else entry


WEB_LINK = re.compile(r"\S*://\S*|\bwww\.\S+", re.IGNORECASE)


def plain(text):
    """Model-written text for a note or reason: no markup, no web links, no payment details.

    Only real web addresses (http://..., www....) are removed. A name that only looks like
    a domain (a product name, "invoice.pdf") is information for the support team and stays.
    """
    text = redact(re.sub(r"<[^<>]{0,200}>", " ", text or ""))  # redact first: an IBAN can look like a link
    return " ".join(WEB_LINK.sub(" ", text).split())


# ---- reply checks ----


# A number next to money or time: "€4", "acht euro", "40%", "60 minuten", "two weeks".
# Such a number must always come from the help centre, never from the ticket.
UNIT = (r"(?:€|euro|eur|dollar|procent|percent|prozent|%|seconden|seconds?|sekunden?|"
        r"minuten|minuut|minutes?|uur|uren|hours?|stunden?|dagen|dag|days?|tagen?|"
        r"weken|week|weeks|wochen?|maanden|maand|months?|monaten?|monate|jaar|jaren|years?|jahren?|jahre)")
# Two patterns, so "kost € 9" finds both "kost €" and "€ 9".
MEASURE = [re.compile(rf"([\w.,]+)\s*{UNIT}(?!\w)", re.IGNORECASE), re.compile(r"[€$£]\s*([\w.,]+)")]
QUOTED_SINGLE = re.compile(r"(?<!\w)['‘«]([^'‘’«»\n]{2,60})['’»](?!\w)")


def fact_findings(draft, cited, ticket_text):
    """What in a draft is not from the help centre, per check: {check name: [items]}.

    cited is the text of the help-centre entries the model quoted. ticket_text is what the
    customer wrote: a reply may repeat a number from it (a version number, say), but a
    number next to money or time must always come from the help centre.
    """
    flat = squash(draft)
    links = urls(draft) | {u.lower().rstrip(".,;:!?\"'>/") for u in re.findall(r"://(\S+)", draft)}
    measured = " ".join(m for pattern in MEASURE for m in pattern.findall(draft))
    names = QUOTED.findall(draft) + QUOTED_SINGLE.findall(draft) + LABEL_AFTER.findall(draft)
    odd_words = [w for w in draft.split() if re.search(r"\w\.\w", w) and not w.isascii()]

    def shown(values):
        return [f"{value:g}" for value in sorted(values)]

    return {
        "link": sorted(links - urls(cited)) + odd_words,
        "number": shown(numbers(draft) - numbers(cited) - numbers(ticket_text)),
        "money_or_time": shown(numbers(measured) - numbers(cited)),
        "menu_name": [name for name in names if squash(name) not in cited],
        "menu_path": [f"{a} → {b}" for a, b in ARROW_STEP.findall(flat) if f"{a} → {b}" not in cited],
    }


def fact_problems(draft, cited, ticket_text):
    """Names of the fact checks a draft fails. Empty list: every fact is from the help centre."""
    return [name for name, items in fact_findings(draft, cited, ticket_text).items() if items]


def cited_text(out, faq):
    """The help-centre entries the model quoted, as one squashed text: the only allowed sources."""
    return squash(" ".join(entry_body(faq.get(i, "")) for i in sorted({q.faq_id for q in out.faq_quotes})))


def repair_hint(out, faq, ticket_text):
    """For the one repair try: exactly what was wrong, in words the model can act on."""
    findings = fact_findings(out.draft_reply or "", cited_text(out, faq), ticket_text)
    lines = [f"{name}: {', '.join(items)}" for name, items in findings.items() if items]
    bad_quotes = [q.quote for q in out.faq_quotes
                  if q.faq_id not in faq or squash(q.quote) not in squash(faq[q.faq_id])]
    if bad_quotes:
        lines.append("quotes not found word for word: " + " | ".join(bad_quotes))
    return "; ".join(lines) or "a quote or fact did not match the help centre"


def reply_checks(out, faq, ticket_text=""):
    """Each check is True when the draft passes it."""
    draft = out.draft_reply or ""
    # Every entry the model quoted word for word is a source: an answer may need two entries
    # (a feature in one, its price in another). An entry that was not quoted is never a source.
    covered_ids = {q.faq_id for q in out.sub_questions if q.covered and q.faq_id}
    quoted_ids = {q.faq_id for q in out.faq_quotes}
    cited = cited_text(out, faq)
    quotes_ok = bool(covered_ids) and covered_ids <= quoted_ids and all(
        q.faq_id in faq and len(q.quote.split()) >= 3 and squash(q.quote) in squash(faq[q.faq_id])
        for q in out.faq_quotes
    )
    # A reply never talks about a must-handoff topic (refund, cancel, ...) and never promises.
    topic_words = any(found(entry["phrases"], draft) for entry in PHRASES["tier1"].values())
    return {
        "quotes": quotes_ok,
        "facts": not fact_problems(draft, cited, ticket_text),
        "promises": not found(PHRASES["banned_in_reply"], draft) and not topic_words,
        "language": detect(draft)[0] == out.language or len(draft.split()) < 8,
        "payment": not has_payment_details(draft),
    }


# These run on the English back-translation, so they can be read and checked in English.
PROMISE_IN_ENGLISH = re.compile(
    r"\b(?:we|i)(?:'ll| will| shall| am going to| are going to| have (?:added|passed|reset|forwarded|"
    r"cancell?ed|refunded|scheduled|fixed|updated|changed|informed|asked|escalated))\b"
    r"|\byou(?:'ll| will) (?:hear|receive|get|be (?:contacted|called|refunded|credited))\b"
    r"|\b(?:refund\w*|call you|contact you|get back to you|tomorrow|today|soon|shortly|"
    r"next (?:week|month|quarter)|january|february|march|april|june|july|august|september|"
    r"october|november|december|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    # Someone will act, or something will happen by a time: a promise in any form.
    r"|\b(?:colleague|someone|somebody|support|team|our|they|it|this)\b[^.!?\n]{0,40}"
    r"\b(?:will|'ll|is going to|are going to)\b"
    r"|\bwill be (?:added|built|fixed|supported|released|available|solved|resolved|refunded|"
    r"changed|updated|corrected|checked|looked at)\b"
    r"|\bwithin (?:\d+|a|an|one|two|three|four|five|a few|several) "
    r"(?:minutes?|hours?|days?|weeks?|months?|business days?|working days?)\b",
    re.IGNORECASE,
)
ALWAYS_KNOWN = {"roosterly", "team", "i"}


def view_matches(draft, english_view):
    """The English back-translation must hold the same numbers and URLs."""
    # Digits only: "beiden" and "two" are the same; spelled numbers are checked by fact_findings.
    return bool(english_view) and (
        numbers(draft, words=False) == numbers(english_view, words=False)
        and urls(draft) == urls(english_view)
    )


def view_problems(draft, english_view, known_text, faq_text=""):
    """Check the English back-translation of a draft. Returns the names of failed checks.

    known_text is the cited help-centre entries plus the ticket. Any capitalised
    word in the middle of a sentence (a name, a menu, a product) must come from there.
    faq_text is the cited help-centre entries only. A phrase that looks like a promise
    but is written in the help centre itself ("no email within 10 minutes?") is allowed.
    """
    problems = [] if view_matches(draft, english_view) else ["numbers_or_links"]
    source = squash(faq_text)
    if any(squash(match.group(0)) not in source
           for match in PROMISE_IN_ENGLISH.finditer(english_view or "")):
        problems.append("promise")
    known = set(re.findall(r"[a-zà-ÿ0-9]+", known_text.lower())) | ALWAYS_KNOWN
    for sentence in re.split(r"(?<=[.!?:])\s+|\n+", english_view or ""):
        for word in re.findall(r"[A-Za-zÀ-ÿ][\w]*", sentence)[1:]:
            if word[0].isupper() and word.lower() not in known:
                problems.append("unknown_name")
                return problems
    return problems


# ---- builders ----


PRIORITY_WHY = {  # the brief's priority table, so every reason says why this priority
    "P1": "many users are blocked, or there is a security or legal risk",
    "P2": "one customer is blocked, or money or the contract is involved",
    "P3": "something is wrong, but there is a workaround",
    "P4": "no urgency",
}
TOPIC_WORDS = {
    "refund": "a refund or disputed charge",
    "cancel": "a cancellation or contract change",
    "gdpr": "a personal data (GDPR/AVG) request",
    "security": "a possible security incident",
    "legal": "a legal threat",
    "churn": "a threat to leave",
}


def topic_words(topics):
    return " and ".join(TOPIC_WORDS.get(topic, topic) for topic in topics)


def rule_reason(cause, category, priority):
    """Two sentences: why a human, then why this category and priority."""
    why = PRIORITY_WHY.get(priority, "see the brief")
    return f"{cause} Handed to a human as {category}, {priority}: {why}."


def english_or_empty(text):
    """Model-written text cleaned for a note, or "" when it is not English."""
    text = plain(text)
    language, lead = detect(text)
    return "" if language != "en" and lead >= 2 else text


def build_note(details, cause, plan, hint=""):
    """Handoff note in English, at most 60 words, never with payment details.

    What the customer wants comes first. Only the details are shortened to fit the 60 words,
    so the reason for the handoff and the plan are always in the note.
    """
    fixed = redact(" ".join(part for part in [f"Why handoff: {cause}", f"Plan: {plan}.", hint] if part))
    room = 60 - len("Customer wants:".split()) - len(fixed.split())
    words = redact(english_or_empty(details)).split()
    details = " ".join(words[:max(room, 0)])
    if len(words) > room:  # shortened: end at the last full sentence, never mid-sentence
        cut = details.rfind(". ")
        details = details[:cut + 1] if cut > 0 else details + " ..."
    return f"Customer wants: {details} {fixed}" if details else fixed


def model_reason(out):
    """The model's own reason, or a code-written one if it is empty or not English."""
    reason = plain(out.reason)
    language, lead = detect(reason)
    if not reason or (language != "en" and lead >= 2):
        reason = f"Decided by the model: {out.action}, {out.category}, {out.priority}."
    # The API asks for one or two sentences.
    return " ".join(re.split(r"(?<=[.!?])\s+", reason)[:2])[:300]


def to_handoff(decision, key, cause, summary="", plan="unknown"):
    """Turn a reply into a handoff after a late check failed."""
    return Decision(
        action="handoff",
        language=decision.language,
        category=decision.category,
        priority=decision.priority,
        reason=rule_reason(cause, decision.category, decision.priority),
        handoff_note=build_note(summary, cause, plan),
        handoff_reason=key,
        faq_ids=decision.faq_ids,
        checks=decision.checks,
        rejected_draft=redact(decision.draft_reply),
    )


def fallback(ticket, guard):
    """The model step failed. Hand off using only what the code knows."""
    topics = guard.tier1
    language, lead = detect(f"{ticket.get('subject')} {ticket.get('body')}")
    category = FALLBACK_CATEGORY.get(topics[0], "other") if topics else "other"
    priority = apply_floor("P3", topics)
    found_text = f" Guard found: {', '.join(topics)}." if topics else ""
    return Decision(
        action="handoff",
        language=language if lead >= 2 else "nl",
        category=category,
        priority=priority,
        reason=f"Automated triage failed, so a human must read this ticket. "
        f"Handed off as {category}, {priority}.",
        handoff_note=f"Automated triage failed, please triage by hand.{found_text} "
        f"Plan: {redact(str(ticket.get('customer_plan') or 'unknown'))}.",
        handoff_reason="model_failed",
    )


# ---- the decision ----


@dataclass
class Case:
    """Everything the rules look at for one ticket."""

    ticket: dict
    guard: object
    out: ModelOutput
    faq: dict
    threshold: float

    def __post_init__(self):
        self.plan = self.ticket.get("customer_plan") or "unknown"
        self.text = strip_html(f"{self.ticket.get('subject') or ''}\n{self.ticket.get('body') or ''}")
        self.faq_ids = sorted({q.faq_id for q in self.out.sub_questions if q.covered and q.faq_id})
        # Must-handoff topics: found by the code guard OR named by the model.
        self.topics = list(self.guard.tier1)
        if self.out.must_handoff_topic not in ("none", *self.topics):
            self.topics.append(self.out.must_handoff_topic)

    def handoff(self, key, cause, category=None, priority=None, details=True, hint=""):
        """A handoff by a code rule. The model's own English note (what the customer wants,
        key facts, next step) is kept after the rule, unless details=False."""
        category, priority = category or self.out.category, priority or self.out.priority
        # The model's own note only when it also chose handoff; a reply's note says "no handoff needed".
        own_note = english_or_empty(self.out.handoff_note) if self.out.action == "handoff" else ""
        text = (own_note or self.out.summary) if details else ""
        return Decision(
            action="handoff", language=self.out.language, category=category, priority=priority,
            reason=rule_reason(cause, category, priority),
            handoff_note=build_note(text, cause, self.plan, hint),
            handoff_reason=key, faq_ids=self.faq_ids,
        )


def injection(case):
    if case.guard.injection:
        also = f", and it is about {topic_words(case.topics)}" if case.topics else ""
        priority = "P3" if case.out.priority == "P4" else case.out.priority
        return case.handoff(
            "injection",
            f"The ticket tries to give the agent instructions, which were not followed{also}.",
            priority=apply_floor(priority, case.topics),
            details=False,  # never repeat injected text
        )


def must_handoff_topic(case):
    if case.topics:
        hint = f"Suggested answer for the how-to part: FAQ {', '.join(case.faq_ids)}." if case.faq_ids else ""
        return case.handoff(
            "must_handoff:" + ",".join(case.topics),
            f"Client rule: {topic_words(case.topics)} must always go to a human.",
            priority=apply_floor(case.out.priority, case.topics),
            hint=hint,
        )


def too_long(case):
    if case.guard.too_long:
        return case.handoff("too_long", "The ticket is too long for the model to read in full.")


def outage(case):
    if case.out.many_users_blocked:
        return case.handoff(
            "outage", "Many users or locations are blocked right now.",
            category="bug", priority="P1",
        )


def plan_change(case):
    if case.out.asks_plan_change:  # a change to the plan is a contract change
        return case.handoff(
            "plan_change", "The customer asks us to change their plan, which is a contract change.",
            category="billing", priority=apply_floor(case.out.priority, ["cancel"]),
        )


def close(case):
    """Close needs both the code signal and the model. If only one says close, a human looks."""
    code_says, model_says = case.guard.close_signal, case.out.action == "close"
    if code_says and model_says:
        return Decision(
            action="close", language=case.out.language, category="other", priority="P4",
            reason=f"The ticket is {code_says}. Closed without action as other, P4: no urgency.",
            confidence=case.out.confidence,
        )
    if code_says or model_says:
        # When only the code sees spam or empty, the model's category and priority stand.
        return case.handoff(
            "close_disagree",
            f"Close check disagreed: code says {code_says or 'not empty or spam'}, "
            f"model says {'close' if model_says else 'do not close'}.",
            category="other" if model_says else None,
            priority="P4" if model_says else None,
        )


def not_covered(case):
    questions = case.out.sub_questions
    if not questions or not all(q.covered for q in questions):
        return case.handoff("not_covered", "The help centre does not fully answer this ticket.")


def already_tried(case):
    if case.out.already_tried or found(PHRASES["already_tried"], case.text):
        return case.handoff("already_tried", "The customer already tried the help-centre steps.")


def language(case):
    """We only reply in Dutch, English or German, and only when sure which."""
    ticket_language, lead = detect(case.text)
    draft_language, draft_lead = detect(case.out.draft_reply or "")
    if case.out.language == "other":
        return case.handoff("language", "The ticket is not in Dutch, English or German.")
    if lead >= 3 and ticket_language != case.out.language:
        return case.handoff("language", "Code and model disagree on the ticket language.")
    if case.out.action == "reply" and lead >= 1 and draft_lead >= 1 and draft_language != ticket_language:
        return case.handoff("language", "The draft is not in the language the customer wrote in.")


def model_chose_handoff(case):
    out = case.out
    if out.action == "reply" and (out.draft_reply or "").strip():
        return None
    if out.action != "handoff":  # the model chose reply but wrote no draft
        return case.handoff("model", "The model chose a reply but wrote no draft.")
    note = english_or_empty(out.handoff_note)
    if not note or len(note.split()) > 60:
        note = build_note(out.summary, "The model chose handoff.", case.plan)
    return Decision(
        action="handoff", language=out.language, category=out.category, priority=out.priority,
        reason=model_reason(out), handoff_note=note, confidence=out.confidence,
        handoff_reason="model", faq_ids=case.faq_ids,
    )


def checked_reply(case):
    """The last rule: reply only if every check passes and the model is sure enough."""
    out = case.out
    checks = reply_checks(out, case.faq, case.text)
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        decision = case.handoff("check_failed:" + ",".join(failed),
                                f"The draft reply failed a safety check ({', '.join(failed)}).")
    elif out.confidence < case.threshold:
        decision = case.handoff("low_confidence", "The model was not sure enough to reply.")
    else:
        sources = sorted(set(case.faq_ids) | {q.faq_id for q in out.faq_quotes if q.faq_id in case.faq})
        decision = Decision(
            action="reply", language=out.language, category=out.category, priority=out.priority,
            reason=model_reason(out), draft_reply=out.draft_reply, confidence=out.confidence,
            faq_ids=sources,
        )
    decision.checks = checks
    if decision.action == "handoff":
        decision.rejected_draft = redact(out.draft_reply)
    # Only a wrong name, link, number or quote may be repaired. A promise, a payment detail or a
    # wrong language goes straight to a human.
    if failed and set(failed) <= {"facts", "quotes"}:
        decision.repair_hint = redact(repair_hint(out, case.faq, case.text))
    return decision


RULES = [injection, must_handoff_topic, too_long, outage, plan_change, close,
         not_covered, already_tried, language, model_chose_handoff, checked_reply]


def decide(ticket, guard, out, faq, threshold):
    """Return the final Decision. `out` is None when the model step failed."""
    if out is None:
        return fallback(ticket, guard)
    case = Case(ticket, guard, out, faq, threshold)
    decision = next(d for d in (rule(case) for rule in RULES) if d is not None)
    decision.model_confidence = out.confidence
    if decision.action != "reply":  # replies are checked by the language rule instead
        language, lead = detect(case.text)
        if lead >= 2 and language in ("nl", "en", "de"):
            decision.language = language
    return decision
