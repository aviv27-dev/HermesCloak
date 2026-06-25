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
        )
