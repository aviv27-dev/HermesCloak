import re
from hermescloak.entities import EntitySource
from hermescloak.span import Span

_PROCLITICS = ("ל", "ב", "ו", "מ", "ה", "ש", "כ")
_WORD = re.compile(r"[^\s.,;:!?()\[\]{}\"'/\\־\-]+", re.UNICODE)


class GazetteerRecognizer:
    """Multi-token name match, order-independent + proclitic-aware.

    Proclitic handling is VOCABULARY-GUIDED: a leading proclitic letter is stripped
    from a text token only when (a) the raw token is not itself a known name token and
    (b) the stripped form IS a known name token. This catches glued prefixes ("לאלה")
    without damaging real words that legitimately start with a proclitic ("בית")."""

    def __init__(self, source: EntitySource) -> None:
        self._by_len: dict[int, dict[frozenset[str], str]] = {}
        self._vocab: set[str] = set()
        names = []
        for surface, etype in source.names():
            toks = [m.group(0) for m in _WORD.finditer(surface)]
            if toks:
                names.append((toks, etype))
                self._vocab.update(toks)
        for toks, etype in names:
            self._by_len.setdefault(len(toks), {})[frozenset(toks)] = etype
        self._lengths = sorted(self._by_len.keys(), reverse=True)  # longest-match first

    def _stripped(self, token: str) -> bool:
        return (token not in self._vocab and len(token) >= 3
                and token[0] in _PROCLITICS and token[1:] in self._vocab)

    def _norm(self, token: str) -> str:
        return token[1:] if self._stripped(token) else token

    def recognize(self, text: str) -> list[Span]:
        word_spans = [(m.start(), m.end(), m.group(0)) for m in _WORD.finditer(text)]
        spans: list[Span] = []
        used = [False] * len(word_spans)
        for n in self._lengths:
            table = self._by_len[n]
            for i in range(len(word_spans) - n + 1):
                if any(used[i:i + n]):
                    continue
                window = word_spans[i:i + n]
                key = frozenset(self._norm(w[2]) for w in window)
                etype = table.get(key)
                if etype:
                    start, end = window[0][0], window[-1][1]
                    # if the first text token carried a glued proclitic, leave that
                    # leading letter in the sentence — the entity starts after it.
                    if self._stripped(window[0][2]):
                        start += 1
                    spans.append(Span(start, end, etype, text[start:end]))
                    for j in range(i, i + n):
                        used[j] = True
        spans.sort(key=lambda s: s.start)
        return spans
