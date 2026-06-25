import re
from hermescloak.span import Span

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+972[\-\s]?|0)(?:[2-9]\d?)[\-\s]?\d{3}[\-\s]?\d{4}(?!\d)")
_DIGITS9 = re.compile(r"(?<!\d)\d{9}(?!\d)")
# label-anchored ID / company number that MAY carry separators (hyphens/dots/spaces):
# e.g. "ח.פ 51-454362-3", "ת.ז 12345678-9", "ע.מ: 514543623". Capture the digit group,
# strip separators, validate the 9-digit Israeli check-digit.
_ID_LABELED = re.compile(
    r'(?P<label>ח[.\"״\']?\s*פ|ת[.\"״\']?\s*ז|ע[.\"״\']?\s*מ|עוסק\s+מורשה|מספר\s+חברה|ח\.?\s*ל\.?\s*צ)'
    r'\s*[:#.\-]?\s*(?P<num>\d[\d.\-/ ]{7,13}\d)'
)
# real credit cards = 13-19 CONTIGUOUS digits, or uniform 4-digit groups ("4111 1111 1111 1111").
# NOT a separator after every digit (that spanned unrelated numbers in CRM dumps → 52 false matches).
_CARD = re.compile(r"(?<!\d)(?:\d{13,19}|\d{4}(?:[ -]\d{4}){2,4})(?!\d)")
_CASE = re.compile(r'(?:פש"?ר|חדל"?פ|הוצל"?פ|ת"?א|ה"?פ|תיק)\s*:?\s*(\d{4,9})')
_GUSH = re.compile(r"גוש\s*\d+\s*חלקה\s*\d+")


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
    case numbers, gush/helka. Language-independent."""

    def recognize(self, text: str) -> list[Span]:
        spans: list[Span] = []
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
