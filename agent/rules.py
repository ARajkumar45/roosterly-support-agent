"""Safety rules in plain code. No model is involved here.

- guard: finds must-handoff topics, weak hints, and injection attempts
- redact: hides IBANs and card numbers
- close_signal: says if a ticket looks empty or like spam
- priority floor: code may raise a priority, never lower it

Every phrase lives in rules_phrases.json with its English meaning.
"""

import html
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

PHRASES = json.loads((Path(__file__).parent / "rules_phrases.json").read_text())

# ---- text helpers ----


# Only real HTML tag names are removed. A customer who types "< 10 medewerkers"
# or puts a sentence between < and > must never lose text.
HTML_TAG = re.compile(
    r"</?(?:p|br|div|span|b|i|u|a|strong|em|ul|ol|li|table|tbody|tr|td|th|h[1-6]|html|"
    r"body|head|font|img|blockquote|pre|code|hr|style|script)\b[^<>]*>",
    re.IGNORECASE,
)
MAX_TICKET_CHARS = 6000  # the model sees at most this much of one ticket


def strip_html(text):
    """Remove HTML tags and turn entities like &amp; back into characters."""
    return html.unescape(HTML_TAG.sub(" ", text or ""))


def fold(text):
    """Text without accents, so "beëindigen" and "beeindigen" are the same word."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))


def phrase_pattern(phrase):
    """A whole-phrase match. 'contract' must not match inside 'contracturen'."""
    words = [re.escape(word) for word in fold(phrase).split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


def found(phrases, text):
    """The phrases from the list that appear in the text, with or without accents."""
    text = fold(text)
    return [p for p in phrases if phrase_pattern(p).search(text)]


# ---- guard ----

# Dutch splits this verb: "ik zeg mijn abonnement op" = "I cancel my subscription".
# Match "zeg ... op" only when abonnement, contract or account is in between.
SPLIT_CANCEL = re.compile(
    r"(?<!\w)zeg(?:gen)?(?:\W+\w+){0,6}?\W+(?:abonnement|contract|account)"
    r"(?:\W+\w+){0,6}?\W+op(?!\w)",
    re.IGNORECASE,
)

# The same verb at the end of a clause with no object: "..., dan zeg ik op." = "..., then I cancel."
# Both word orders: "dan zeg ik op" and "wij zeggen op".
PRONOUN = r"(?:ik|we|wij|jij|je|hij|zij|ze)"
SPLIT_CANCEL_END = re.compile(
    rf"(?<!\w)(?:zeg(?:t|gen)?\s+{PRONOUN}|{PRONOUN}\s+zeg(?:t|gen)?)\s+op\s*(?:[.!?,;]|$)",
    re.IGNORECASE | re.MULTILINE,
)

# A cancel word that also fits an employee ("een medewerker heeft opgezegd") only counts
# as a customer cancelling when the subscription or account is named in the same sentence.
CANCEL_OBJECT = r"(?:abonnement|account|roosterly\S*|licentie|pakket|subscription|abo|vertrag)"
CANCEL_WORD = r"(?:opzeggen|opgezegd|opzegging|kundigen|kundige|gekundigt|kundigung)"
OBJECT_CANCEL = re.compile(
    rf"(?<!\w){CANCEL_OBJECT}[^.!?\n]{{0,80}}?(?<!\w){CANCEL_WORD}(?!\w)"
    rf"|(?<!\w){CANCEL_WORD}[^.!?\n]{{0,80}}?(?<!\w){CANCEL_OBJECT}(?!\w)",
    re.IGNORECASE,
)

# Someone else logged in: "iemand heeft op ons account ingelogd", "someone logged in", "jemand hat sich angemeldet".
# Only within one sentence and without "not", so "Can someone help? I am not logged in." does not match.
STRANGER = r"(?:iemand|someone|somebody|jemand|onbekend\w*|unknown|fremd\w*|vreemd\w*)"
LOGGED_IN = r"(?:ingelogd|logged\s+in(?:to)?|eingeloggt|angemeldet)"
SAME_SENTENCE = r"[^\w.!?\n]+(?:\w+[^\w.!?\n]+)"
STRANGER_LOGIN = re.compile(
    rf"(?<!\w){STRANGER}{SAME_SENTENCE}{{0,8}}?{LOGGED_IN}(?!\w)"
    rf"|(?<!\w){LOGGED_IN}{SAME_SENTENCE}{{0,4}}?(?:door|by|von)\s+(?:een\s+|a\s+|an\s+|einen\s+|einem\s+)?"
    rf"{STRANGER}(?!\w)"
    # "ingelogd vanaf een apparaat dat wij niet kennen", "logged into from a device we do not recognise"
    rf"|(?<!\w){LOGGED_IN}{SAME_SENTENCE}{{0,10}}?(?:niet|not|nicht)\s+(?:\w+\s+)?"
    rf"(?:kennen|herkennen|know|recogni[sz]e|kenne|erkennen)(?!\w)",
    re.IGNORECASE,
)
# "niet ingelogd", "not logged in": the customer is logged out, nobody broke in.
NOT_LOGGED_IN = re.compile(rf"(?<!\w)(?:niet|not|nicht|nooit|never)\s+{LOGGED_IN}", re.IGNORECASE)


def stranger_logged_in(text):
    return any(not NOT_LOGGED_IN.search(m.group(0)) for m in STRANGER_LOGIN.finditer(text))


# "Hoe kan ik opzeggen?", "Ich möchte kündigen": the writer cancels. Not when the sentence
# is about staff ("ik wil weten hoe een medewerker die heeft opgezegd ...").
FIRST_PERSON_CANCEL = re.compile(
    rf"(?<!\w)(?:ik|we|wij|ich|wir)[^\w.!?\n]+(?:\w+[^\w.!?\n]+){{0,6}}?{CANCEL_WORD}(?!\w)",
    re.IGNORECASE,
)
STAFF = re.compile(r"(?<!\w)(?:medewerker\w*|werknemer\w*|collega\w*|personeel|employee\w*|"
                   r"staff|mitarbeiter\w*|kollege\w*)(?!\w)", re.IGNORECASE)


def customer_cancels(folded):
    """A cancel word that names the subscription, or a writer who cancels (not about staff)."""
    if OBJECT_CANCEL.search(folded):
        return True
    return any(not STAFF.search(m.group(0)) for m in FIRST_PERSON_CANCEL.finditer(folded))


# Text that tries to instruct the agent, in its common forms. Fixed phrases are in
# rules_phrases.json; these patterns catch the variations ("all previous", "the above").
INJECTION = re.compile(
    r"(?<!\w)(?:ignore|disregard|forget|negeer|vergeet|ignoreer|ignoriere|vergiss)\W+"
    r"(?:\w+\W+){0,4}?(?:instructions?|instructies?|regels|rules|prompt|anweisungen|befehle)(?!\w)"
    r"|(?<!\w)(?:systeem|system)\s*(?:bericht|message|nachricht|melding|prompt)(?!\w)"
    r"|(?<!\w)(?:note|message|bericht|notitie|hinweis|nachricht)\s+(?:for|to|voor|aan|fur|an)\s+"
    r"(?:the\s+|de\s+|het\s+|die\s+|den\s+)?(?:ai|ki|a\.i\.)?[\s-]*"
    r"(?:assistant|assistent|chatbot|bot|agent|model)(?!\w)",
    re.IGNORECASE,
)

# Anything that looks like the tag we wrap ticket text in (see llm.py).
TAG_LOOKALIKE = re.compile(r"<\s*/?\s*ticket_\w*", re.IGNORECASE)


@dataclass
class Guard:
    tier1: list = field(default_factory=list)  # must-handoff topics found
    tier2: list = field(default_factory=list)  # weak hints, never force a handoff
    injection: bool = False
    close_signal: str | None = None  # "empty", "spam" or None
    too_long: bool = False  # the model cannot see all of it, so a human must


HIDDEN_CHARS = re.compile("[\u200b-\u200d\u2060\ufeff\u00ad]")  # zero-width, soft hyphen
ANY_SHORT_TAG = re.compile(r"<[^<>\n]{0,80}>")


def normalise(text):
    """Same letters in one form, and no invisible characters between them."""
    return HIDDEN_CHARS.sub("", unicodedata.normalize("NFKC", text or ""))


def guard(subject, body):
    """Check the full subject and body. Nothing is cut off or skipped."""
    raw = normalise(f"{subject or ''}\n{body or ''}")
    # Three views of the same text, so a phrase cannot hide behind markup:
    # known HTML removed, untouched, and every short <...> removed.
    text = "\n".join([strip_html(raw), raw, html.unescape(ANY_SHORT_TAG.sub("", raw))])

    tier1 = [
        topic
        for topic, entry in PHRASES["tier1"].items()
        if found(entry["phrases"], text)
    ]
    folded = fold(text)
    cancels = SPLIT_CANCEL.search(text) or SPLIT_CANCEL_END.search(text) or customer_cancels(folded)
    if "cancel" not in tier1 and cancels:
        tier1.append("cancel")
    if "security" not in tier1 and stranger_logged_in(text):
        tier1.append("security")

    injection = bool(
        found(PHRASES["injection"], text) or INJECTION.search(folded)
        or TAG_LOOKALIKE.search(html.unescape(raw))
    )
    result = Guard(tier1, found(PHRASES["tier2"], text), injection)
    result.too_long = len(strip_html(raw)) > MAX_TICKET_CHARS
    if not tier1 and not injection:
        result.close_signal = close_signal(subject, body)
    return result


# ---- redaction ----

SEP = r"[\s.\-/_,]{0,3}"  # spaces, dots, dashes, slashes, underscores, commas or a line break
# Country code and check digits written together (NL91 ...), then 11 to 30 more characters.
IBAN = re.compile(rf"\b[A-Z]{{2}}\d{{2}}(?:{SEP}[A-Z0-9]){{11,30}}", re.IGNORECASE)
# Written apart, like "NL 91 ...". This also looks like normal text ("op 12 ..."),
# so it only counts when the IBAN checksum is right.
IBAN_SPACED = re.compile(
    rf"\b[A-Z]{{2}}[\s.\-/]{{1,3}}\d{{2}}(?:{SEP}[A-Z0-9]){{11,30}}", re.IGNORECASE
)
# After the first four characters an IBAN is a short bank code and then many digits.
IBAN_SHAPE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{0,6}\d{8,}")
# Glued to a word ("rekeningNL91ABNA..."): a capital country code right after a small letter.
# Normal text never looks like this, but it only counts when the IBAN checksum is right.
IBAN_GLUED = re.compile(rf"(?<=[a-z])[A-Z]{{2}}\d{{2}}(?:{SEP}[A-Z0-9]){{11,30}}")
# 13 to 19 digits, with optional separators between them.
CARD = re.compile(rf"(?<!\d)\d(?:{SEP}\d){{12,18}}(?!\d)")
REDACTED = "[redacted]"


def iban_checksum_ok(candidate):
    """The standard IBAN check: move 4 characters to the end, letters become
    numbers (A=10 ... Z=35), and the result modulo 97 must be 1.
    Words after the IBAN may be part of the candidate, so whole blocks are
    dropped from the end, one at a time."""
    blocks = re.split(r"[\s.\-/_,]+", candidate.upper())
    for keep in range(len(blocks), 0, -1):
        part = "".join(blocks[:keep])
        if 15 <= len(part) <= 34:
            digits = "".join(str(int(char, 36)) for char in part[4:] + part[:4])
            if int(digits) % 97 == 1:
                return True
    return False


def iban_end(text, need_checksum):
    """Where the IBAN in this candidate ends, or None if it is not an IBAN."""
    compact = re.sub(r"[^A-Z0-9]", "", text.upper())
    if not IBAN_SHAPE.match(compact) or (need_checksum and not iban_checksum_ok(text)):
        return None
    # Hide up to the last digit, so a normal word after the IBAN is kept.
    return max(index for index, char in enumerate(text) if char.isdigit()) + 1


def card_end(text):
    """A card number is one block of digits, or blocks of four (or 4-6-5) typed
    with the same separator each time. A date range, a phone number or opening
    hours have other block sizes or mixed separators and are left alone."""
    sizes = [len(block) for block in re.split(r"[\s.\-/_,]+", text)]
    separators = {" " if sep.isspace() else sep for sep in re.findall(r"[\s.\-/_,]+", text)}
    shaped = (all(size == 4 for size in sizes[:-1]) and sizes[-1] <= 4) or sizes == [4, 6, 5]
    if len(sizes) == 1 or (shaped and len(separators) == 1):
        return len(text)
    # Any other grouping or mixed separators (blocks of 4, 8 and 4 digits): still a card when the
    # digits pass the Luhn check that every real card number passes. A date or phone number
    # rarely does, and hiding one by mistake is the safe error.
    # Blocks shorter than 4 digits (dates like 01-02-2026) never count here.
    return len(text) if min(sizes) >= 4 and luhn_ok(re.sub(r"\D", "", text)) else None


def luhn_ok(digits):
    """The standard card-number checksum."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char) * (2 if index % 2 else 1)
        total += number - 9 if number > 9 else number
    return 13 <= len(digits) <= 19 and total % 10 == 0


def hide(pattern, text, find_end):
    """Replace every real match. A rejected candidate is re-scanned one character on,
    so a real number that starts inside it is still found."""
    parts, position = [], 0
    while match := pattern.search(text, position):
        end = find_end(match.group(0))
        if end is None:
            parts.append(text[position : match.start() + 1])
            position = match.start() + 1
        else:
            parts.append(text[position : match.start()] + REDACTED)
            position = match.start() + end
    return "".join(parts) + text[position:]


def prepare(text):
    """The form redaction works on: invisible characters removed, wide digits made normal,
    and quote marks or indents at the start of a wrapped line taken away, so a number
    split over two quoted lines ("> NL91 ABNA\n> 0417 ...") is still one number."""
    return re.sub(r"[ \t]*\n[ \t>]*", "\n", normalise(text))


def redact(text):
    """Hide IBANs and card numbers. Used on every text that leaves the program."""
    if not text:
        return text
    text = prepare(text)
    text = hide(IBAN, text, lambda found: iban_end(found, need_checksum=False))
    text = hide(IBAN_SPACED, text, lambda found: iban_end(found, need_checksum=True))
    text = hide(IBAN_GLUED, text, lambda found: iban_end(found, need_checksum=True))
    return hide(CARD, text, card_end)


def has_payment_details(text):
    return bool(text) and redact(text) != prepare(text)


# ---- close signal ----

def meaningful_text(subject, body):
    """Subject and body without HTML, quoted lines and mobile signatures."""
    lines = []
    for line in strip_html(f"{subject or ''}\n{body or ''}").splitlines():
        line = line.strip()
        if line.startswith(">"):
            continue
        if any(line.lower().startswith(s.lower()) for s in PHRASES["signatures"]):
            continue
        lines.append(line)
    return "\n".join(lines)


def close_signal(subject, body):
    """'empty', 'spam' or None. Code alone never closes; the model must agree."""
    text = meaningful_text(subject, body)
    letters = sum(char.isalpha() for char in text)
    if letters < 3:
        return "empty"
    # A link alone is never a spam signal: real customers send screenshots and signatures.
    if found(PHRASES["spam"], text):
        return "spam"
    return None


# ---- priority and category ----

# The lowest priority a topic may get. Code only raises, never lowers.
PRIORITY_FLOOR = {
    "security": "P1",
    "legal": "P1",
    "outage": "P1",
    "refund": "P2",
    "cancel": "P2",
    "churn": "P2",
    "gdpr": "P2",
}


def apply_floor(priority, topics):
    """Return the more urgent of the given priority and the topics' floors."""
    floors = [PRIORITY_FLOOR[t] for t in topics if t in PRIORITY_FLOOR]
    return min([priority, *floors])  # "P1" sorts before "P2"


# Used only when the model step failed and code must pick a category.
FALLBACK_CATEGORY = {
    "refund": "billing",
    "cancel": "billing",
    "gdpr": "other",
    "security": "other",
    "legal": "other",
    "churn": "other",
}
