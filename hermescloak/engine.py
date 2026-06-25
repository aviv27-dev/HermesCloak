import copy
from dataclasses import dataclass, field
from hermescloak.detection import DetectionEngine
from hermescloak.entities import EntitySource
from hermescloak.instruction import TOKEN_INSTRUCTION
from hermescloak.profile import Profile
from hermescloak.pseudonymizer import pseudonymize
from hermescloak.recognizers.deterministic import DeterministicRecognizer
from hermescloak.recognizers.gazetteer import GazetteerRecognizer
from hermescloak.restorer import leftover_tokens, restore_json, restore_text
from hermescloak.vault import Vault


@dataclass
class RestoreReport:
    leftover: list[str] = field(default_factory=list)
    restored_any: bool = False


class Engine:
    """Per-session facade. One Engine instance per agent session (owns one Vault)."""

    def __init__(self, profile: Profile, entity_source: EntitySource | None = None,
                 extra_recognizers: list | None = None, vault: Vault | None = None) -> None:
        self.profile = profile
        # inject a DurableVault to persist the map across restarts; defaults to in-memory.
        self.vault = vault if vault is not None else Vault()
        recognizers: list = [DeterministicRecognizer()]
        if entity_source is not None:
            recognizers.append(GazetteerRecognizer(entity_source))
        # NER (extra) is SECONDARY — yields to deterministic/gazetteer spans on overlap so
        # the curated, proclitic-aware gazetteer wins over NER spans that include a glued prefix.
        self.detection = DetectionEngine(recognizers, never_mask=profile.never_mask,
                                         secondary=extra_recognizers or [])
        # per-content cache: detection (incl. slow NER) runs once per unique message content,
        # not once per message per API call — essential when re-tokenizing a long history each call.
        # Safe because the vault makes value→token stable, so identical content → identical output.
        self._content_cache: dict[str, str] = {}

    def _tokenize_cached(self, content: str) -> str:
        cached = self._content_cache.get(content)
        if cached is not None:
            return cached
        out = pseudonymize(content, self.detection, self.vault)
        self._content_cache[content] = out
        return out

    def sanitize_outbound(self, messages: list[dict]) -> list[dict]:
        out = copy.deepcopy(messages)              # never mutate the canonical conversation
        for msg in out:
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = self._tokenize_cached(content)
        if self.profile.token_instruction and not self.vault.is_empty():
            # merge into an existing system message if present (hermes already has one),
            # else insert a fresh leading system message
            for msg in out:
                if msg.get("role") == "system" and isinstance(msg.get("content"), str):
                    msg["content"] = TOKEN_INSTRUCTION + "\n\n" + msg["content"]
                    break
            else:
                out.insert(0, {"role": "system", "content": TOKEN_INSTRUCTION})
        # persist any new mappings issued this turn (no-op for the in-memory Vault)
        getattr(self.vault, "save", lambda: None)()
        return out

    def restore_inbound(self, response: dict) -> tuple[dict, RestoreReport]:
        out = copy.deepcopy(response)
        report = RestoreReport()
        if isinstance(out.get("content"), str):
            out["content"] = restore_text(out["content"], self.vault)
        for tc in out.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if "arguments" in fn:
                fn["arguments"] = restore_json(fn["arguments"], self.vault)
        # leftover scan across all restored surfaces (the fail-safe signal)
        surfaces: list[str] = []
        if isinstance(out.get("content"), str):
            surfaces.append(out["content"])
        for tc in out.get("tool_calls") or []:
            surfaces.append(str((tc.get("function") or {}).get("arguments", "")))
        seen: list[str] = []
        for s in surfaces:
            for t in leftover_tokens(s, self.vault):
                if t not in seen:
                    seen.append(t)
        report.leftover = seen
        report.restored_any = not self.vault.is_empty()
        return out, report
