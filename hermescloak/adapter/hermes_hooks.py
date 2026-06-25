"""Integration helpers for hermes-agent. CODE-ONLY — importing this module does NOT touch
hermes and does NOT wire anything. Actual installation (deployment into a live agent) is a
separate, explicitly-approved step.

Verified seams (hermes-agent, 2026-06-18):
  OUTBOUND  agent/chat_completion_helpers.py :: build_api_kwargs (~L555)
            single transport-agnostic choke point (covers chat-completions, anthropic, codex,
            bedrock). Tokenize a COPY of the messages here, before transport.build_kwargs().
  INBOUND   agent/conversation_loop.py, right after `normalize_response` (~L1367)
            where the assistant message (content + tool_calls args) converges for all
            transports. Restore here, before anything is persisted / executed / sent.

Why a library, not a proxy: hermes' codex transport is non-standard streaming; no
OpenAI-compatible proxy can wrap it. So we call these helpers in-process at the seams.

`session_id_getter` is a zero-arg callable the integrator supplies to read the current
agent's session id at call time."""
from typing import Callable
from hermescloak.adapter.guard import CloakGuard


def make_outbound_hook(guard: CloakGuard, session_id_getter: Callable[[], str]):
    """Returns f(api_messages)->api_messages for the build_api_kwargs seam."""
    def _hook(api_messages: list[dict]) -> list[dict]:
        return guard.sanitize_outbound(api_messages, session_id_getter())
    return _hook


def make_inbound_hook(guard: CloakGuard, session_id_getter: Callable[[], str]):
    """Returns f(response_dict)->response_dict for the post-normalize_response seam.

    `response_dict` must expose {"content": str, "tool_calls": [{"function": {"arguments": ...}}]}.
    The integrator adapts the hermes NormalizedResponse object to/from this shape."""
    def _hook(response: dict) -> dict:
        return guard.restore_inbound(response, session_id_getter())
    return _hook
