import re
from hermescloak.span import Span

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+972[\-\s]?|0)(?:[2-9]\d?)[\-\s]?\d{3}[\-\s]?\d{4}(?!\d)")
_DIGITS9 = re.compile(r"(?<!\d)\d{9}(?!\d)")
# label-anchored ID / company number that MAY carry separators (hyphens/dots/spaces):
# e.g. "ח.פ 51-454362-3", "ת.ז 12345678-9", "ע.מ: 514543623". Capture the digit group,
# strip separators, validate the 9-digit Israeli check-digit.
# Q: a label's quote/geresh — also in its JSON-escaped form (ת\"ז inside a tool result).
_Q = r"(?:\\?[.\"״'׳])"
_ID_LABELED = re.compile(
    r'(?P<label>ח' + _Q + r'?\s*פ|ת' + _Q + r'?\s*ז|ע' + _Q + r'?\s*מ|עוסק\s+מורשה|מספר\s+חברה|ח\.?\s*ל\.?\s*צ)'
    r'\s*[:#.\-]?\s*(?P<num>\d[\d.\-/ ]{7,13}\d)'
)
# real credit cards = 13-19 CONTIGUOUS digits, or uniform 4-digit groups ("4111 1111 1111 1111").
# NOT a separator after every digit (that spanned unrelated numbers in CRM dumps → 52 false matches).
_CARD = re.compile(r"(?<!\d)(?:\d{13,19}|\d{4}(?:[ -]\d{4}){2,4})(?!\d)")
# Israeli court-file prefixes (explicit list — a generic "X"Y" acronym would also catch ש"ח).
# The number is captured WITH its "-MM-YY" suffix (e.g. ת"פ 34224-11-23), not just the head.
_CASE_PREFIXES = ("פש|ר", "חדל|פ", "הוצל|פ", "ת|א", "ה|פ", "ת|פ", "תמ|ש", "ע|פ", "ע|א", "בש|א", "רע|א",
                  "רע|פ", "בג|ץ", "עת|מ", "עע|מ", "תא|מ", "תא|ק", "ת|צ", "צ|א", "ת|ט", "ע|ח", "מ|ת",
                  "ת|ק", "עב|ל", "ס|ע")
# the quote may be ", ״ or (inside a JSON-encoded tool result) \"
_CASE_PREFIX = "|".join(a + r'(?:\\?["״])?' + b for a, b in (p.split("|") for p in _CASE_PREFIXES)) + "|תיק"
_CASE = re.compile(r'(?:' + _CASE_PREFIX + r')\s*(?:מס\\?[\'׳]?\s*)?:?\s*(\d{4,9}(?:-\d{1,2}-\d{2})?)(?![\d-])')
_GUSH = re.compile(r"גוש\s*\d+\s*חלקה\s*\d+")
# Street address: a street keyword + name + house number, optionally ", <city>". Matches inside a
# glued proclitic too ("ברחוב הגפן 5", "לרחוב ..."). Over-masking a following word as "city" is
# accepted (identity leaks are the costly failure).
_ADDRESS = re.compile(
    r"(?:רחוב|רח['׳]|שדרות|שד['׳]|סמטת|כיכר|(?<=מען: )דרך|(?<=מען:)דרך|(?<=כתובת: )דרך)\s+[^\d\n\\·,;:()]{2,30}?\s*\d{1,4}(?:\s*[/א-ת]\b)?"
    r"(?:\s*,\s*[א-ת][א-ת\-–]*(?:\s[א-ת][א-ת\-–]*){0,2})?"
)
# Bank account number after its label (Hebrew) or an Israeli IBAN.
_BANK = re.compile(r"(?:חשבון|ח-ן|חש['׳])\s*(?:בנק\s*)?(?:מס['׳.]?\s*|מספר\s*)?:?\s*(\d{4,12})(?![\d-])")
_IBAN = re.compile(r"\bIL\d{2}(?:\s?\d{4}){4}\s?\d{3}\b")
# Date of birth after its label.
_DOB = re.compile(
    r"(?:ילידת|יליד|ת\.?\s*לידה|תאריך\s+לידה|נולדה?\s+ב(?:יום)?)\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
)
# Israeli vehicle plates: 123-45-678 / 12-345-67 (the dash layout makes them distinctive).
_PLATE = re.compile(r"(?<![\d-])(?:\d{3}-\d{2}-\d{3}|\d{2}-\d{3}-\d{2})(?![\d-])")


# Credentials (API keys, tokens, private keys). High-precision vendor formats only, plus a
# key=value form whose VALUE (not the key name) is masked. Entity type "סוד" (secret).
_SECRETS = re.compile(
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[\s\S]{20,}?-----END (?:[A-Z]+ )?PRIVATE KEY-----"
    r"|\bsk-ant-[A-Za-z0-9_\-]{20,}"
    r"|\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}"
    r"|\bxox[abposr]-[A-Za-z0-9\-]{10,}"
    r"|\bAIza[0-9A-Za-z_\-]{35}"
    r"|\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}"
    r"|\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b"
    r"|\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
)
_SECRET_ASSIGN = re.compile(
    r"(?i)\b(?:password|passwd|pwd|secret|api[_\-]?key|access[_\-]?token|auth[_\-]?token|client[_\-]?secret)"
    r"\\?[\"']?\s*[:=]\s*\\?[\"']?(?P<val>[^\s\"',;⟦⟧\\]{8,})"
)


# separator-tolerant 9-digit ID/company number ANYWHERE (e.g. "51-073338-1", "310.733.381").
# No whitespace in the class, so it can't span across unrelated numbers; validated by check-digit.
_SEP_ID = re.compile(r"(?<![\d.\-/])\d[\d.\-/]{7,12}\d(?![\d.\-/])")


def _valid_israeli_id(d: str) -> bool:
    if len(d) != 9 or not d.isdigit():
        return False
    total = 0
    for i, ch in enumerate(d):
        x = int(ch) * (1 if i % 2 == 0 else 2)
        total += x if x < 10 else x - 9
    return total % 10 == 0


def _luhn(num: str) -> bool:
    digits = [int(c) for c in num if c.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class DeterministicRecognizer:
    """Universal/structured PII: IL id (check-digit), phone, email, credit (Luhn),
    case numbers, gush/helka, credentials. Language-independent."""

    def __init__(self, secrets: bool = True) -> None:
        self._secrets = secrets

    def recognize(self, text: str) -> list[Span]:
        spans: list[Span] = []
        if self._secrets:
            for m in _SECRETS.finditer(text):
                spans.append(Span(m.start(), m.end(), "סוד", m.group(0)))
            for m in _SECRET_ASSIGN.finditer(text):
                spans.append(Span(m.start("val"), m.end("val"), "סוד", m.group("val")))
        for m in _EMAIL.finditer(text):
            spans.append(Span(m.start(), m.end(), "מייל", m.group(0)))
        for m in _PHONE.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) == 9 and _valid_israeli_id(digits):
                continue  # a valid 9-digit Israeli ID, not a phone — let the ID detectors label it
            spans.append(Span(m.start(), m.end(), "טלפון", m.group(0)))
        for m in _CASE.finditer(text):
            spans.append(Span(m.start(1), m.end(1), "תיק", m.group(1)))
        for m in _GUSH.finditer(text):
            spans.append(Span(m.start(), m.end(), "גושחלקה", m.group(0)))
        for m in _ADDRESS.finditer(text):
            spans.append(Span(m.start(), m.end(), "כתובת", m.group(0).rstrip()))
        for m in _BANK.finditer(text):
            spans.append(Span(m.start(1), m.end(1), "חשבון", m.group(1)))
        for m in _IBAN.finditer(text):
            spans.append(Span(m.start(), m.end(), "חשבון", m.group(0)))
        for m in _DOB.finditer(text):
            spans.append(Span(m.start(1), m.end(1), "לידה", m.group(1)))
        for m in _PLATE.finditer(text):
            spans.append(Span(m.start(), m.end(), "רכב", m.group(0)))
        for m in _CARD.finditer(text):
            if _luhn(m.group(0)):
                spans.append(Span(m.start(), m.end(), "אשראי", m.group(0)))
        for m in _ID_LABELED.finditer(text):
            num = m.group("num")
            digits = re.sub(r"\D", "", num)
            if len(digits) == 9 and _valid_israeli_id(digits):
                etype = "תז" if m.group("label").startswith("ת") else "חפ"
                spans.append(Span(m.start("num"), m.end("num"), etype, num))
        for m in _SEP_ID.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) == 9 and _valid_israeli_id(digits):
                # bare number: type is ambiguous (ת"ז vs ח.פ) — use a NEUTRAL token so we never
                # assert a wrong type; the model reads the real type from the cleartext field.
                spans.append(Span(m.start(), m.end(), "מזהה", m.group(0)))
        for m in _DIGITS9.finditer(text):
            if _valid_israeli_id(m.group(0)):
                spans.append(Span(m.start(), m.end(), "מזהה", m.group(0)))
        # dedupe identical (start,end) spans, keeping the first (more specific label wins)
        seen: set[tuple[int, int]] = set()
        unique: list[Span] = []
        for s in spans:
            key = (s.start, s.end)
            if key in seen:
                continue
            seen.add(key)
            unique.append(s)
        return unique
