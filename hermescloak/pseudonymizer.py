from hermescloak.detection import DetectionEngine
from hermescloak.vault import Vault


def pseudonymize(text: str, engine: DetectionEngine, vault: Vault) -> str:
    spans = engine.detect(text)
    # replace right-to-left so earlier offsets stay valid
    for s in sorted(spans, key=lambda s: s.start, reverse=True):
        token = vault.tokenize(s.text, s.entity_type)
        text = text[:s.start] + token + text[s.end:]
    return text
