from dataclasses import dataclass, field
import yaml


@dataclass
class Profile:
    name: str
    languages: list[str] = field(default_factory=lambda: ["he", "en"])
    fail_mode: str = "open"            # "open" | "closed"
    never_mask: list[str] = field(default_factory=list)
    token_instruction: bool = True
    alerts_on: list[str] = field(default_factory=lambda: ["unfiltered_sent", "leftover_token"])
    detect_secrets: bool = True        # mask API keys / tokens / private keys / password=...
    tolerant_restore: bool = True      # also restore lightly mangled tokens (⟦לקוח 1⟧, [לקוח_1])
    replay_cache: bool = True          # replay the model's own tokenized turns byte-identically
    audit_new_pii: bool = True         # audit PII the model introduced / originals it echoed
    vault_ttl_hours: float = 24.0

    @classmethod
    def from_yaml(cls, path: str) -> "Profile":
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        alerts = (data.get("alerts") or {}).get("on", ["unfiltered_sent", "leftover_token"])
        return cls(
            name=data.get("profile", "default"),
            languages=data.get("languages", ["he", "en"]),
            fail_mode=data.get("fail_mode", "open"),
            never_mask=data.get("never_mask", []),
            token_instruction=data.get("token_instruction", True),
            alerts_on=alerts,
            detect_secrets=bool(data.get("detect_secrets", True)),
            tolerant_restore=bool(data.get("tolerant_restore", True)),
            replay_cache=bool(data.get("replay_cache", True)),
            audit_new_pii=bool(data.get("audit_new_pii", True)),
            vault_ttl_hours=float(data.get("vault_ttl_hours", 24.0)),
        )
