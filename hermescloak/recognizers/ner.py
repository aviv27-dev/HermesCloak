from hermescloak.span import Span


class HebrewNerRecognizer:
    """Optional plugin. Maps DictaBERT-NER PER spans to entity_type 'לקוח'.
    Attribution: dicta-il/dictabert-ner (CC BY 4.0). Requires the [ner] extra."""

    _PER_LABELS = {"PER", "PERS", "PERSON"}

    def __init__(self, model: str = "dicta-il/dictabert-ner") -> None:
        from transformers import pipeline  # lazy import; only when NER is actually used
        self._pipe = pipeline("ner", model=model, aggregation_strategy="simple")

    def recognize(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for ent in self._pipe(text):
            label = str(ent.get("entity_group", "")).upper()
            if label in self._PER_LABELS:
                start, end = int(ent["start"]), int(ent["end"])
                spans.append(Span(start, end, "לקוח", text[start:end]))
        return spans
