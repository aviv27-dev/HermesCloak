from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    entity_type: str
    text: str
