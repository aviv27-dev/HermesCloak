from dataclasses import dataclass, field
import yaml


@dataclass
class Profile:
    name: str
    languages: list[str] = field(default_factory=lambda: ["he", "en"])
    fail_mode: str = "open"            # "open" | "closed"
    never_mask: list[str] = field(default_factory=list)
    # institutional mail domains (suffix match) that are not personal data; [] masks every address
    never_mask_domains: list[str] = field(default_factory=lambda: ["gov.il", "muni.il", "knesset.il", "idf.il"])
    token_instruction: bool = True
    alerts_on: list[str] = field(default_factory=lambda: ["unfiltered_sent", "leftover_token"])
    detect_secrets: bool = True        # mask API keys / tokens / private keys / password=...
    tolerant_restore: bool = True      # also restore lightly mangled tokens (⟦לקוח 1⟧, [לקוח_1])
    replay_cache: bool = True          # replay the model's own tokenized turns byte-identically
    audit_new_pii: bool = True         # audit PII the model introduced / originals it echoed
    vault_ttl_hours: float = 24.0
    # Optional second opinion on the ALREADY-MASKED request from a typed-decision model (Jev via
    # OpenRouter, key OPENROUTER_API_KEY): "is a private person's name / contact / identifier still
    # in clear?" — the two gaps regexes and a client list cannot close. Off by default.
    jev_check: bool = False
    jev_min_confidence: float = 0.7
    jev_action: str = "audit"          # "audit" (log only) | "block" (withhold the request, like fail_mode closed)
    jev_timeout_s: float = 3.0
    jev_max_chars: int = 600           # level B: at most this much (masked) text per call
    decide_backend: str = "jev"        # "jev" (OpenRouter) | "local" (the office model, /v1/systemone)
    decide_shadow: bool = False        # also ask the other backend in the background and audit both

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
            never_mask_domains=list(data.get("never_mask_domains", ["gov.il", "muni.il", "knesset.il", "idf.il"]) or []),
            token_instruction=data.get("token_instruction", True),
            alerts_on=alerts,
            detect_secrets=bool(data.get("detect_secrets", True)),
            tolerant_restore=bool(data.get("tolerant_restore", True)),
            replay_cache=bool(data.get("replay_cache", True)),
            audit_new_pii=bool(data.get("audit_new_pii", True)),
            vault_ttl_hours=float(data.get("vault_ttl_hours", 24.0)),
            jev_check=bool(data.get("jev_check", False)),
            jev_min_confidence=float(data.get("jev_min_confidence", 0.7)),
            jev_action=str(data.get("jev_action", "audit")),
            jev_timeout_s=float(data.get("jev_timeout_s", 3.0)),
            jev_max_chars=int(data.get("jev_max_chars", 600)),
            decide_backend=str(data.get("decide_backend", "jev")),
            decide_shadow=bool(data.get("decide_shadow", False)),
        )
