"""The hermes plugin against FAKE hermes modules shaped like current hermes-agent
(agent.transports registry, StreamDeliveryMixin, auxiliary_client funnel). A real-hermes
end-to-end run is described in docs/INTEGRATION.md."""
import asyncio
import json
import random
import sys
import types
from dataclasses import dataclass

import pytest

import hermescloak.adapter.hermes_live as live


@dataclass
class _TC:
    id: str
    name: str
    arguments: str
    function = property(lambda self: self)


@dataclass
class _NR:
    content: str | None
    tool_calls: list | None
    finish_reason: str = "stop"
    reasoning: str | None = None


def _fake_hermes(monkeypatch):
    agent_pkg = types.ModuleType("agent")
    agent_pkg.__path__ = []
    tr = types.ModuleType("agent.transports")
    tr._REGISTRY = {}

    class ChatTransport:
        def normalize_response(self, response, **kw):
            return _NR(content=response["content"], tool_calls=[_TC(**t) for t in response.get("tool_calls", [])],
                       reasoning=response.get("reasoning"))

    tr._REGISTRY["chat_completions"] = ChatTransport
    tr._discover_transports = lambda: None
    tr.register_transport = lambda mode, cls: tr._REGISTRY.__setitem__(mode, cls)
    tr.get_transport = lambda mode: tr._REGISTRY[mode]()

    sd = types.ModuleType("agent.stream_delivery")

    class StreamDeliveryMixin:
        def _fire_stream_delta(self, text):
            self.got.append(text)

        def _emit_stream_end(self, *, final_text="", finished=True, error=None):
            self.ended = True

        def _reset_stream_delivery_tracking(self):
            pass

    sd.StreamDeliveryMixin = StreamDeliveryMixin

    ac = types.ModuleType("agent.auxiliary_client")

    def _relay_sync_completion(client, kwargs, *, provider=None, api_mode=None, create=None):
        return create(kwargs)

    async def _relay_async_completion(client, kwargs, *, provider=None, api_mode=None, create=None):
        return create(kwargs)

    ac._relay_sync_completion = _relay_sync_completion
    ac._relay_async_completion = _relay_async_completion
    for name, mod in {"agent": agent_pkg, "agent.transports": tr, "agent.stream_delivery": sd,
                      "agent.auxiliary_client": ac}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    agent_pkg.transports, agent_pkg.stream_delivery, agent_pkg.auxiliary_client = tr, sd, ac
    return tr, sd, ac


class _Ctx:
    def __init__(self):
        self.middleware = {}

    def register_middleware(self, kind, cb):
        self.middleware.setdefault(kind, []).append(cb)


@pytest.fixture
def env(tmp_path, monkeypatch):
    live._ENGINES.clear()
    home = tmp_path / "home"
    cloak = home / "cloak"
    cloak.mkdir(parents=True)
    (cloak / "MODE").write_text("enforce", encoding="utf-8")
    (cloak / "gazetteer.txt").write_text("שירה לוי\tלקוח\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    fakes = _fake_hermes(monkeypatch)
    import hermescloak.hermes_plugin as plugin
    ctx = _Ctx()
    plugin.register(ctx)
    return plugin, ctx, fakes, cloak


def _send(ctx, request, api_mode="chat_completions"):
    (cb,) = ctx.middleware["llm_request"]
    res = cb(request=request, original_request=request, session_id="s1", api_mode=api_mode,
             middleware_schema_version="hermes.middleware.v1")
    return res["request"] if res else request


def test_register_reports_all_points_ok(env):
    plugin, ctx, _, cloak = env
    st = plugin.status()
    assert all(v.startswith("ok") for v in st.values()), st
    log = (cloak / "audit.log").read_text(encoding="utf-8")
    assert "plugin_active" in log and "seam_missing" not in log


def test_full_round_trip_outbound_inbound(env):
    _, ctx, (tr, _, _), cloak = env
    req = {"model": "m", "messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "שירה לוי, 050-1234567"}]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "f", "arguments": '{"to": "050-1234567"}'}}]}]}
    sent = _send(ctx, req)
    blob = json.dumps(sent, ensure_ascii=False)
    assert "שירה לוי" not in blob and "050-1234567" not in blob
    assert "⟦לקוח_1⟧" in blob and "⟦טלפון_1⟧" in blob
    # model reply: tokens in content, reasoning, and \u-escaped inside tool args
    nr = tr.get_transport("chat_completions").normalize_response({
        "content": "call ⟦לקוח_1⟧", "reasoning": "about ⟦טלפון_1⟧",
        "tool_calls": [{"id": "c2", "name": "f",
                        "arguments": json.dumps({"to": "⟦טלפון_1⟧", "q": 'say "hi"'})}]})
    assert nr.content == "call שירה לוי" and nr.reasoning == "about 050-1234567"
    assert json.loads(nr.tool_calls[0].arguments) == {"to": "050-1234567", "q": 'say "hi"'}
    assert '\\"leftover\\": 0' in (cloak / "audit.log").read_text(encoding="utf-8")


def test_transport_registered_later_is_wrapped(env):
    _, ctx, (tr, _, _), _ = env
    _send(ctx, {"messages": [{"role": "user", "content": "שירה לוי"}]})

    class LateTransport:
        def normalize_response(self, response, **kw):
            return _NR(content=response, tool_calls=None)

    tr.register_transport("late", LateTransport)
    assert tr.get_transport("late").normalize_response("hi ⟦לקוח_1⟧").content == "hi שירה לוי"


def test_streaming_any_chunking_and_end_flush(env):
    _, ctx, (_, sd, _), _ = env
    _send(ctx, {"messages": [{"role": "user", "content": "שירה לוי 050-1234567"}]})
    full = "Hi ⟦לקוח_1⟧ / ⟦טלפון_1⟧ — literal ⟦ kept ⟦"
    want = "Hi שירה לוי / 050-1234567 — literal ⟦ kept ⟦"
    rnd = random.Random(7)
    for _ in range(100):
        a = sd.StreamDeliveryMixin()
        a.got, a.session_id = [], "s1"
        a._reset_stream_delivery_tracking()
        i = 0
        while i < len(full):
            n = rnd.randint(1, 6)
            a._fire_stream_delta(full[i:i + n])
            i += n
        a._emit_stream_end(final_text="", finished=True, error=None)
        assert "".join(a.got) == want and a.ended


def test_auxiliary_calls_are_cloaked_both_ways(env):
    _, _, (_, _, ac), _ = env
    seen = {}

    def create(req):
        seen["req"] = req
        return {"choices": [{"message": {"content": "summary of ⟦לקוח_1⟧", "tool_calls": None}}]}

    r = ac._relay_sync_completion(None, {"messages": [{"role": "user", "content": "compress: שירה לוי"}]},
                                  create=create)
    assert "שירה לוי" not in json.dumps(seen["req"], ensure_ascii=False)
    assert r["choices"][0]["message"]["content"] == "summary of שירה לוי"
    r2 = asyncio.run(ac._relay_async_completion(None, {"messages": [{"role": "user", "content": "שירה לוי"}]},
                                                create=create))
    assert r2["choices"][0]["message"]["content"] == "summary of שירה לוי"


def test_shadow_and_off_send_original(env):
    _, ctx, _, cloak = env
    req = {"messages": [{"role": "user", "content": "שירה לוי"}]}
    for mode in ("shadow", "off"):
        (cloak / "MODE").write_text(mode, encoding="utf-8")
        assert _send(ctx, req) is req


def test_fail_closed_withholds_content(env, monkeypatch):
    _, ctx, _, cloak = env
    (cloak / "profile.yaml").write_text("profile: x\nfail_mode: closed\n", encoding="utf-8")
    live._ENGINES.clear()

    def boom(*a, **k):
        raise RuntimeError("detector crashed")

    monkeypatch.setattr("hermescloak.engine.Engine.tokenize_text", boom)
    sent = _send(ctx, {"messages": [{"role": "user", "content": "שירה לוי"}]})
    assert "שירה לוי" not in json.dumps(sent, ensure_ascii=False)
    assert "blocked_send" in (cloak / "audit.log").read_text(encoding="utf-8")


def test_fail_open_default_sends_original(env, monkeypatch):
    _, ctx, _, cloak = env
    monkeypatch.setattr("hermescloak.engine.Engine.tokenize_text",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    req = {"messages": [{"role": "user", "content": "שירה לוי"}]}
    assert _send(ctx, req) is req
    assert "unfiltered_sent" in (cloak / "audit.log").read_text(encoding="utf-8")


def test_missing_hermes_target_is_reported_not_raised(tmp_path, monkeypatch):
    live._ENGINES.clear()
    (tmp_path / "cloak").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _fake_hermes(monkeypatch)
    del sys.modules["agent.auxiliary_client"]._relay_sync_completion   # simulate a hermes refactor
    import hermescloak.hermes_plugin as plugin
    monkeypatch.setattr(plugin, "_STATUS", {})
    plugin.register(_Ctx())
    assert plugin.status()["auxiliary"].startswith("missing")
    assert "seam_missing" in (tmp_path / "cloak" / "audit.log").read_text(encoding="utf-8")
