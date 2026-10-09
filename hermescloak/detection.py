import re
from typing import Protocol
from hermescloak.span import Span

# Literal escape sequences inside JSON-encoded text (a tool result is often a JSON string):
# "\\nאיתי כהן" must not glue the "n" onto the name. Replaced by spaces of the SAME length for
# detection only, so every span offset still points into the original text.
_ESCAPES = re.compile(r"\\[nrt]")

_PROCLITICS = ("ל", "ב", "ו", "מ", "ה", "ש", "כ")


def _variants(s: str) -> set[str]:
    """A surface form plus its single-leading-proclitic-stripped variant.
    Lets a never-mask entry "בית המשפט" also match a glued "לבית המשפט"."""
    out = {s}
    if len(s) >= 3 and s[0] in _PROCLITICS:
        out.add(s[1:])
    return out


def _overlaps(a: Span, b: Span) -> bool:
    return a.start < b.end and b.start < a.end


class Recognizer(Protocol):
    def recognize(self, text: str) -> list[Span]: ...


class DetectionEngine:
    """Primary recognizers (deterministic + gazetteer) take precedence over secondary ones
    (e.g. NER): a secondary span is dropped if it overlaps a primary span. Keeps the curated,
    proclitic-aware gazetteer spans winning over NER spans that may include a glued proclitic
    (NER "לשירה לוי" vs gazetteer "שירה לוי")."""

    def __init__(self, recognizers: list[Recognizer], never_mask: list[str] | None = None,
                 secondary: list[Recognizer] | None = None) -> None:
        self._primary = recognizers
        self._secondary = secondary or []
        self._never: set[str] = set()
        for entry in (never_mask or []):
            self._never |= _variants(entry)

    def _is_never(self, span: Span) -> bool:
        return bool(_variants(span.text) & self._never)

    def _collect(self, recs: list[Recognizer], text: str) -> list[Span]:
        spans: list[Span] = []
        for r in recs:
            spans.extend(r.recognize(text))
        return [s for s in spans if not self._is_never(s)]

    @staticmethod
    def _resolve(spans: list[Span]) -> list[Span]:
        spans.sort(key=lambda s: (s.start, -(s.end - s.start)))
        kept: list[Span] = []
        last_end = -1
        for s in spans:
            if s.start >= last_end:
                kept.append(s)
                last_end = s.end
        return kept

    @staticmethod
    def _normalized(text: str) -> str:
        return _ESCAPES.sub("  ", text) if "\\" in text else text

    @staticmethod
    def _rebase(spans: list[Span], text: str) -> list[Span]:
        """Span texts from the ORIGINAL string (detection may have run on the normalized copy)."""
        return [s if text[s.start:s.end] == s.text else Span(s.start, s.end, s.entity_type, text[s.start:s.end])
                for s in spans]

    def detect_primary(self, text: str) -> list[Span]:
        """Deterministic + gazetteer only (no NER): cheap enough for per-response audits."""
        norm = self._normalized(text)
        return sorted(self._rebase(self._resolve(self._collect(self._primary, norm)), text),
                      key=lambda s: s.start)

    def detect(self, text: str) -> list[Span]:
        norm = self._normalized(text)
        kept = self._resolve(self._collect(self._primary, norm))
        for s in self._resolve(self._collect(self._secondary, norm)):
            if not any(_overlaps(s, k) for k in kept):
                kept.append(s)
        kept.sort(key=lambda s: s.start)
        return self._rebase(kept, text)
