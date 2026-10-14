from hermescloak.detection import DetectionEngine
from hermescloak.span import Span
from hermescloak.vault import Vault

KNOWN_MIN_LEN = 4   # never mask very short fragments just because they were once a value


def known_value_spans(text: str, vault: Vault) -> list[Span]:
    """Every occurrence of a value the vault already holds — even where no detector fired on that
    occurrence (a label-anchored address repeated later without its label, a name in a context
    the gazetteer misses). A value caught once is masked everywhere."""
    reals = getattr(vault, "known_values", lambda: [])()
    spans: list[Span] = []
    for real in reals:
        if len(real) < KNOWN_MIN_LEN or real not in text:
            continue
        start = 0
        while (i := text.find(real, start)) != -1:
            spans.append(Span(i, i + len(real), "known", real))
            start = i + 1
    return spans


def pseudonymize(text: str, engine: DetectionEngine, vault: Vault) -> str:
    detected = engine.detect(text)
    # pass 1: learn this text's values (left to right → natural token numbering)
    for s in detected:
        vault.tokenize(s.text, s.entity_type)
    # pass 2: detected spans + every occurrence of any known value; longest/earliest wins
    spans = DetectionEngine._resolve(list(detected) + known_value_spans(text, vault))
    # replace right-to-left so earlier offsets stay valid
    for s in sorted(spans, key=lambda s: s.start, reverse=True):
        token = vault.tokenize(s.text, s.entity_type)
        text = text[:s.start] + token + text[s.end:]
    return text
