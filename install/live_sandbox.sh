#!/usr/bin/env bash
# One-shot sandbox: install the current hermes-agent + HermesCloak into a throwaway venv and run
# install/live_model_check.py against a real model (default Ollama Cloud; needs OLLAMA_API_KEY in
# the environment and network access to ollama.com). Never touches a real hermes deployment.
#
#   bash install/live_sandbox.sh            # WORK dir defaults to a temp dir
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$(mktemp -d -t hermescloak-sandbox-XXXX)}"
PY="${PY:-3.13}"
echo "[sandbox] work dir: $WORK"
command -v uv >/dev/null || { echo "[sandbox] needs uv (https://docs.astral.sh/uv/)"; exit 2; }

if [ ! -d "$WORK/hermes-agent" ]; then
  git clone -q --depth 50 https://github.com/NousResearch/hermes-agent.git "$WORK/hermes-agent"
fi
uv venv -q -p "$PY" "$WORK/venv"
export VIRTUAL_ENV="$WORK/venv"
# hermes pins its deps for Python 3.14; install the same packages unpinned for this interpreter
# (Windows-only packages dropped)
"$WORK/venv/bin/python" - "$WORK/hermes-agent/pyproject.toml" > "$WORK/reqs.txt" <<'EOF'
import re, sys, tomllib
deps = tomllib.load(open(sys.argv[1], "rb"))["project"]["dependencies"]
for d in deps:
    name = re.split(r"[=<>;\[ ]", d.split(";")[0].strip(), maxsplit=1)[0]
    extra = re.search(r"\[[^\]]+\]", d.split(";")[0])
    if re.search(r"pywin32|winpty|winrt|pyobjc", name, re.I):
        continue
    print(name + (extra.group(0) if extra else ""))
EOF
uv pip install -q -r "$WORK/reqs.txt"
uv pip install -q --no-deps -e "$WORK/hermes-agent"
uv pip install -q -e "$REPO[dev]"
if [ "${SANDBOX_INSTALL_ONLY:-}" = "1" ]; then
  echo "[sandbox] installed: $WORK/venv (hermes-agent at $WORK/hermes-agent)"
  exit 0
fi
"$WORK/venv/bin/python" "$REPO/install/apply_hooks.py" --verify --hermes-root "$WORK/hermes-agent" >/dev/null 2>&1 || true
echo "[sandbox] offline e2e (fake cloud):"
"$WORK/venv/bin/python" "$REPO/install/e2e_check.py" --hermes-root "$WORK/hermes-agent" --provider openai | grep -E "FAIL|PROTECTED"
echo "[sandbox] live model:"
"$WORK/venv/bin/python" "$REPO/install/live_model_check.py" --hermes-root "$WORK/hermes-agent"
