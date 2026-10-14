#!/usr/bin/env python3
"""Small helpers for the GitHub Actions sandboxes (no third-party imports).

  python install/ci_helpers.py models                 # Ollama Cloud model ids, one per line
  python install/ci_helpers.py check-model NAME       # exit 1 (with the list) if NAME is not offered
  python install/ci_helpers.py telegram TEXT          # send TEXT to every TELEGRAM_ALLOWED_USERS id
  python install/ci_helpers.py telegram --file PATH   # …the text of a file instead

Credentials come from the environment (OLLAMA_API_KEY, TELEGRAM_BOT_TOKEN) and are never printed.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

OLLAMA_MODELS = "https://ollama.com/v1/models"
TELEGRAM_SEND = "https://api.telegram.org/bot{token}/sendMessage"


def models() -> list[str]:
    key = os.environ.get("OLLAMA_API_KEY", "")
    req = urllib.request.Request(OLLAMA_MODELS, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return sorted(m["id"] for m in json.load(r)["data"])
    except Exception:
        return []


def check_model(name: str) -> int:
    ids = models()
    print("Available models:")
    for i in ids:
        print("  " + i)
    if ids and name not in ids:
        print(f"::error::model '{name}' is not offered by Ollama Cloud — set the repository variable "
              "HC_LIVE_MODEL to one of the names above")
        return 1
    return 0


def telegram(text: str) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    users = [u for u in os.environ.get("TELEGRAM_ALLOWED_USERS", "").replace(",", " ").split() if u]
    if not token or not users:
        return 0
    for chat in users:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(TELEGRAM_SEND.format(token=token), data=data),
                                   timeout=30).read()
        except Exception:
            pass                                    # a notification never fails the job
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "models":
        print("\n".join(models()))
        return 0
    if cmd == "check-model" and rest:
        return check_model(rest[0])
    if cmd == "telegram" and rest:
        if rest[0] == "--file" and len(rest) > 1:
            try:
                with open(rest[1], encoding="utf-8") as fh:
                    return telegram(fh.read())
            except OSError:
                return 0
        return telegram(" ".join(rest))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
