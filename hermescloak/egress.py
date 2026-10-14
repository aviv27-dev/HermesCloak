"""Egress-side restore — the last-mile safety net.

The inbound seam restores ⟦tokens⟧ in the model's REPLY. But a model working in
token-space can write a *script* that CONSTRUCTS a token from char codes
(``chr(0x27E6)+'מזהה_1'+chr(0x27E7)`` == ⟦מזהה_1⟧) and then send/write it — the
literal token never appears in the reply, so reply-restore can't catch it.

This module restores at the EGRESS point instead: any tool/helper/script — even a
separate process the model wrote — calls ``restore_content`` (or the CLI) on the
*actual* outbound content (email body, file bytes, message text) right before it
leaves. At that moment the token is finally LITERAL, so it is caught.

It works cross-process because the per-agent vault is on DISK
(``$HERMES_HOME/cloak/vaults/*.json``). This module only READS the vault files
(it never mutates/expires them) and merges ALL of them, so a token minted in any
session/restart is restorable. If a token cannot be restored it is reported as
``leftover`` (and audited) so it never leaves silently.
"""
import json
import os
import sys
import time
from glob import glob

from hermescloak.tokens import TOKEN_RE, find_tokens


def _home(hermes_home: str | None) -> str:
    return hermes_home or os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _vault_dir(hermes_home: str | None) -> str:
    return os.path.join(_home(hermes_home), "cloak", "vaults")


def _egress_key(hermes_home: str | None):
    k = os.environ.get("HERMESCLOAK_VAULT_KEY", "").strip()
    if k:
        return k
    ref = os.environ.get("HERMESCLOAK_VAULT_KEY_FILE", "").strip()
    if not ref:
        try:
            with open(os.path.join(_home(hermes_home), "cloak", "vault_key_file"), encoding="utf-8") as f:
                ref = f.read().strip()
        except OSError:
            ref = ""
    if ref:
        try:
            with open(os.path.expanduser(ref), encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None
    return None


def load_token_map(hermes_home: str | None = None) -> dict[str, str]:
    """Merge token->real from ALL vault files for this agent (read-only). Reads encrypted
    vaults when the key is available, and falls back to a vault's .bak if the main file is
    unreadable (e.g. caught mid-crash)."""
    from hermescloak.durable_vault import read_journal, read_payload
    key = _egress_key(hermes_home)
    token_to_real: dict[str, str] = {}
    for path in sorted(glob(os.path.join(_vault_dir(hermes_home), "*.json"))):
        for candidate in (path, path + ".bak"):
            try:
                data = read_payload(candidate, key)
            except Exception:
                continue
            for tok, real in (data.get("token_to_real") or {}).items():
                token_to_real.setdefault(tok, real)   # first (oldest) wins; all should agree
            break
        try:                                          # mappings not yet compacted into the snapshot
            for e in read_journal(path + ".journal", key)[0]:
                token_to_real.setdefault(e["t"], e["r"])
        except Exception:
            pass
    return token_to_real


def restore_content(content: str, hermes_home: str | None = None,
                    token_map: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Restore every ⟦token⟧ in `content` to its real value using the on-disk vault.
    Returns (restored_content, leftover_tokens). Never raises (fail-safe)."""
    if not isinstance(content, str) or "⟦" not in content:
        return content, []
    tm = token_map if token_map is not None else load_token_map(hermes_home)
    restored = TOKEN_RE.sub(lambda m: tm.get(m.group(0), m.group(0)), content)
    leftover = [t for t in find_tokens(restored) if t not in tm]
    return restored, leftover


def _audit_leftover(leftover: list[str], hermes_home: str | None, where: str) -> None:
    if not leftover:
        return
    try:
        log = os.path.join(_home(hermes_home), "cloak", "audit.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "egress_leftover", "ts": time.strftime("%F %T"),
                                "where": where, "tokens": leftover}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def restore_file(path: str, hermes_home: str | None = None) -> list[str]:
    """Restore a text file IN PLACE. Returns leftover tokens (also audited)."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return []
    restored, leftover = restore_content(content, hermes_home)
    if restored != content:
        with open(path, "w", encoding="utf-8") as f:
            f.write(restored)
    _audit_leftover(leftover, hermes_home, f"file:{os.path.basename(path)}")
    return leftover


def restore_for_send(content: str, hermes_home: str | None = None, where: str = "send") -> str:
    """Convenience for outbound helpers (email/message/calendar): returns the restored
    string and audits any leftover. Use right before the actual send."""
    restored, leftover = restore_content(content, hermes_home)
    _audit_leftover(leftover, hermes_home, where)
    return restored


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="hermescloak.egress",
                                 description="Last-mile restore of ⟦tokens⟧ in outbound content.")
    ap.add_argument("action", choices=["restore"])
    ap.add_argument("path", nargs="?", help="file to restore in place; omit + use --stdin for piping")
    ap.add_argument("--stdin", action="store_true", help="read content from stdin, write restored to stdout")
    ap.add_argument("--home", default=None, help="HERMES_HOME (else $HERMES_HOME)")
    a = ap.parse_args(argv)
    if a.stdin or not a.path:
        content = sys.stdin.read()
        restored, leftover = restore_content(content, a.home)
        sys.stdout.write(restored)
        if leftover:
            sys.stderr.write(f"\n[hermescloak] WARNING leftover tokens not restored: {leftover}\n")
            _audit_leftover(leftover, a.home, "stdin")
        return 0
    leftover = restore_file(a.path, a.home)
    if leftover:
        sys.stderr.write(f"[hermescloak] WARNING leftover tokens in {a.path}: {leftover}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
