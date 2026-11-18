"""The decision. The model fills in a form (ModelOutput). This code reads the
form and the guard result and picks the final action: reply, handoff or close.

Order, first match wins:
 1. must-handoff topic or injection          -> handoff
 2. many users blocked right now (outage)    -> handoff, bug, P1
 3. customer asks us to change their plan    -> handoff, billing, P2
 4. close only if code AND model say close   -> close; if only one, handoff
 5. FAQ does not cover it, or already tried  -> handoff
 6. language is not nl, en or de             -> handoff
 7. a reply check fails, or low confidence   -> handoff
 8. otherwise                                -> reply
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


def numbers(text):
    """Numbers as values, so '€4', '€ 4' and '€4,00' are all 4.0.

    Number words count too ("vier weken" is 4), so a number cannot hide as a word.
    A "1." at the start of a line is a list marker, not a fact.
    """
    text = URL.sub(" ", text or "")
    text = re.sub(r"(?m)^\s*\d+[.)]\s", " ", text)
    text = re.sub(r"(?<=\d)\.(?=\d{3}\b)", "", text)  # 1.000 is one thousand
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    spelled = {float(PHRASES["number_words"][w]) for w in words if w in PHRASES["number_words"]}
    return {float(n.replace(",", ".")) for n in NUMBER.findall(text)} | spelled


def entry_body(entry):
    """A help-centre entry without its heading line, so 'F05' is not read as the number 5."""
    return entry.split("\n", 1)[1] if "\n" in entry else entry


def plain(text):
    """Model-written text for a note or reason: no markup, no links, no payment details."""
    text = re.sub(r"<[^<>]{0,200}>", " ", text or "")
    text = re.sub(r"\S*://\S*", " ", text)
    return " ".join(redact(URL.sub(" ", text)).split())


# ---- reply checks ----


def reply_checks(out, faq):
    """Each check is True when the draft passes it."""
    draft = out.draft_reply or ""
    # Only entries that answer a covered question AND were quoted count as sources.
    # Quoting an extra entry must not make its prices and links allowed.
    covered_ids = {q.faq_id for q in out.sub_questions if q.covered and q.faq_id}
    quoted_ids = {q.faq_id for q in out.faq_quotes}
    cited = squash(" ".join(entry_body(faq.get(i, "")) for i in covered_ids & quoted_ids))

    quotes_ok = bool(covered_ids) and covered_ids <= quoted_ids and all(
        q.faq_id in faq
        and len(q.quote.split()) >= 3
        and squash(q.quote) in squash(faq[q.faq_id])
        for q in out.faq_quotes
    )

    flat = squash(draft)
    schemes = {u.lower().rstrip(".,;:!?\"'>/") for u in re.findall(r"://(\S+)", draft)}
    odd_link = any(re.search(r"\w\.\w", word) and not word.isascii() for word in draft.split())
    facts_ok = (
        urls(draft) <= urls(cited)
        and schemes <= urls(cited)
        and not odd_link
        and numbers(draft) <= numbers(cited)
        and all(squash(label) in cited for label in QUOTED.findall(draft))
        and all(f"{a} → {b}" in cited for a, b in ARROW_STEP.findall(flat))
        and all(squash(label) in cited for label in LABEL_AFTER.findall(draft))
    )

    # A reply never talks about a must-handoff topic (refund, cancel, ...) and never promises.
    topic_words = any(found(entry["phrases"], draft) for entry in PHRASES["tier1"].values())
    language, _ = detect(draft)
    return {
        "quotes": quotes_ok,
        "facts": facts_ok,
        "promises": not found(PHRASES["banned_in_reply"], draft) and not topic_words,
        "language": language == out.language or len(draft.split()) < 8,
        "payment": not has_payment_details(draft),
    }


# These run on the English back-translation, so they can be read and checked in English.
PROMISE_IN_ENGLISH = re.compile(
    r"\b(?:we|i)(?:'ll| will| am going to| are going to| have (?:added|passed|reset|forwarded|"
    r"cancell?ed|refunded|scheduled|fixed|updated|changed|informed|asked|escalated))\b"
    r"|\b(?:refund\w*|call you|contact you|get back to you|tomorrow|today|soon|shortly|"
    r"next (?:week|month|quarter)|january|february|march|april|june|july|august|september|"
    r"october|november|december|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
ALWAYS_KNOWN = {"roosterly", "team", "i"}


def view_matches(draft, english_view):
    """The English back-translation must hold the same numbers and URLs."""
    return bool(english_view) and (
        numbers(draft) == numbers(english_view) and urls(draft) == urls(english_view)
    )


def view_problems(draft, english_view, known_text):
    """Check the English back-translation of a draft. Returns the names of failed checks.

    known_text is the cited help-centre entries plus the ticket. Any capitalised
    word in the middle of a sentence (a name, a menu, a product) must come from there.
    """
    problems = [] if view_matches(draft, english_view) else ["numbers_or_links"]
    if PROMISE_IN_ENGLISH.search(english_view or ""):
        problems.append("promise")
    known = set(re.findall(r"[a-zà-ÿ0-9]+", known_text.lower())) | ALWAYS_KNOWN
    for sentence in re.split(r"(?<=[.!?:])\s+|\n+", english_view or ""):
        for word in re.findall(r"[A-Za-zÀ-ÿ][\w]*", sentence)[1:]:
            if word[0].isupper() and word.lower() not in known:
                problems.append("unknown_name")
                return problems
    return problems


# ---- builders ----


def build_note(summary, cause, plan, hint=""):
    """Handoff note in English, at most 60 words, never with payment details."""
    language, lead = detect(summary)
    if language != "en" and lead >= 2:  # the model did not write English
        summary = ""
    summary = plain(summary)
    parts = [
        f"Customer wants: {summary}" if summary else "",
        f"Why handoff: {cause}",
        f"Plan: {plan}.",
        hint,
    ]
    words = redact(" ".join(part for part in parts if part)).split()
    return " ".join(words[:60])


def model_reason(out):
    """The model's own reason, or a code-written one if it is empty or not English."""
    reason = plain(out.reason)
    language, lead = detect(reason)
    if not reason or (language != "en" and lead >= 2):
        reason = f"Decided by the model: {out.action}, {out.category}, {out.priority}."
    return reason[:300]


def to_handoff(decision, key, cause, summary="", plan="unknown"):
    """Turn a reply into a handoff after a late check failed."""
    return Decision(
        action="handoff",
        language=decision.language,
        category=decision.category,
        priority=decision.priority,
        reason=f"{cause} Handed to a human as {decision.category}, {decision.priority}.",
        handoff_note=build_note(summary, cause, plan),
        handoff_reason=key,
        faq_ids=decision.faq_ids,
        checks=decision.checks,
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


def decide(ticket, guard, out, faq, threshold):
    """Return the final Decision. `out` is None when the model step failed."""
    if out is None:
        return fallback(ticket, guard)

    plan = ticket.get("customer_plan") or "unknown"
    text = strip_html(f"{ticket.get('subject') or ''}\n{ticket.get('body') or ''}")
    faq_ids = sorted({q.faq_id for q in out.sub_questions if q.covered and q.faq_id})

    def handoff(key, cause, category=None, priority=None, summary=True, hint=""):
        category, priority = category or out.category, priority or out.priority
        return Decision(
            action="handoff",
            language=out.language,
            category=category,
            priority=priority,
            reason=f"{cause} Handed to a human as {category}, {priority}.",
            handoff_note=build_note(out.summary if summary else "", cause, plan, hint),
            handoff_reason=key,
            faq_ids=faq_ids,
        )

    # 1. Must-handoff topic (code guard OR model) or an injection attempt.
    topics = list(guard.tier1)
    if out.must_handoff_topic != "none" and out.must_handoff_topic not in topics:
        topics.append(out.must_handoff_topic)
    if guard.injection:
        priority = apply_floor("P3" if out.priority == "P4" else out.priority, topics)
        return handoff(
            "injection",
            "The ticket contains text that tries to give the agent instructions. "
            "It was not followed." + (f" Guard also found: {', '.join(topics)}." if topics else ""),
            priority=priority,
            summary=False,  # never repeat injected text
        )
    if topics:
        rules = f"{', '.join(topics)} always go to a human."
        hint = (
            f"Suggested answer for the how-to part: FAQ {', '.join(faq_ids)}."
            if faq_ids
            else ""
        )
        return handoff(
            "must_handoff:" + ",".join(topics),
            f"Client rule: {rules}",
            priority=apply_floor(out.priority, topics),
            hint=hint,
        )

    if guard.too_long:
        return handoff(
            "too_long", "The ticket is too long for the model to read in full."
        )

    # 2. Outage: many users or locations cannot work right now.
    if out.many_users_blocked:
        return handoff(
            "outage",
            "Many users or locations are blocked right now. Check status.roosterly.nl.",
            category="bug",
            priority="P1",
        )

    # 3. The customer asks us to change the plan. That is a contract change.
    if out.asks_plan_change:
        return handoff(
            "plan_change",
            "The customer asks us to change their plan, which is a contract change.",
            category="billing",
            priority=apply_floor(out.priority, ["cancel"]),
        )

    # 4. Close needs both the code signal and the model.
    model_close = out.action == "close"
    if guard.close_signal and model_close:
        return Decision(
            action="close",
            language=out.language,
            category="other",
            priority="P4",
            reason=f"The ticket is {guard.close_signal}. Closed without action, other, P4.",
            confidence=out.confidence,
        )
    if guard.close_signal or model_close:
        # When only the code sees spam or empty, the model's category and priority stand.
        return handoff(
            "close_disagree",
            f"Close check disagreed: code says {guard.close_signal or 'not empty or spam'}, "
            f"model says {'close' if model_close else 'do not close'}.",
            category="other" if model_close else None,
            priority="P4" if model_close else None,
        )

    # 5. The FAQ must cover every question, and the customer must not have tried it.
    if not out.sub_questions or not all(q.covered for q in out.sub_questions):
        return handoff("not_covered", "The help centre does not fully answer this ticket.")
    if out.already_tried or found(PHRASES["already_tried"], text):
        return handoff(
            "already_tried", "The customer already tried the help-centre steps."
        )

    # 6. We only reply in Dutch, English or German, and only when sure which.
    language, lead = detect(text)
    if out.language == "other":
        return handoff("language", "The ticket is not in Dutch, English or German.")
    if lead >= 3 and language != out.language:
        return handoff("language", "Code and model disagree on the ticket language.")

    # 7. The model itself chose handoff, or the draft fails a check.
    if out.action != "reply" or not (out.draft_reply or "").strip():
        note = plain(out.handoff_note)
        note_language, note_lead = detect(note)
        if not note or len(note.split()) > 60 or (note_language != "en" and note_lead >= 2):
            note = build_note(out.summary, "The model chose handoff.", plan)
        return Decision(
            action="handoff",
            language=out.language,
            category=out.category,
            priority=out.priority,
            reason=model_reason(out),
            handoff_note=note,
            confidence=out.confidence,
            handoff_reason="model",
            faq_ids=faq_ids,
        )
    checks = reply_checks(out, faq)
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        decision = handoff(
            "check_failed:" + ",".join(failed),
            f"The draft reply failed a safety check ({', '.join(failed)}).",
        )
        decision.checks = checks
        return decision
    if out.confidence < threshold:
        decision = handoff("low_confidence", "The model was not sure enough to reply.")
        decision.checks = checks
        return decision

    # 8. Reply.
    return Decision(
        action="reply",
        language=out.language,
        category=out.category,
        priority=out.priority,
        reason=model_reason(out),
        draft_reply=out.draft_reply,
        confidence=out.confidence,
        faq_ids=faq_ids,
        checks=checks,
    )
