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

    def register_hook(self, name, cb):
        self.middleware.setdefault("hook:" + name, []).append(cb)


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


def test_fail_closed_default_withholds(env, monkeypatch):
    _, ctx, _, cloak = env
    monkeypatch.setattr("hermescloak.engine.Engine.tokenize_text",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    req = {"messages": [{"role": "user", "content": "שירה לוי"}]}
    out = _send(ctx, req)
    assert "שירה לוי" not in json.dumps(out, ensure_ascii=False)
    audit = (cloak / "audit.log").read_text(encoding="utf-8")
    assert "blocked_send" in audit and "שירה" not in audit


def test_missing_hermes_target_is_reported_not_raised(tmp_path, monkeypatch):
    live._ENGINES.clear()
    (tmp_path / "cloak").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _fake_hermes(monkeypatch)
    del sys.modules["agent.auxiliary_client"]._relay_sync_completion   # simulate a hermes refactor
    import hermescloak.hermes_plugin as plugin
    monkeypatch.setattr(plugin, "_STATUS", {})
    ctx = _Ctx()
    plugin.register(ctx)
    assert plugin.status()["auxiliary"].startswith("missing")
    assert "seam_missing" in (tmp_path / "cloak" / "audit.log").read_text(encoding="utf-8")
    # auxiliary calls (titles, compression) would leak: in enforce the turn is withheld instead
    (tmp_path / "cloak" / "MODE").write_text("enforce", encoding="utf-8")
    (mw,) = ctx.middleware["llm_request"]
    out = mw(request={"messages": [{"role": "user", "content": "שירה לוי"}]}, session_id="s1")
    assert out and "שירה לוי" not in json.dumps(out, ensure_ascii=False)
    (tmp_path / "cloak" / "profile.yaml").write_text("profile: x\nfail_mode: open\n", encoding="utf-8")
    assert mw(request={"messages": [{"role": "user", "content": "שירה לוי"}]}, session_id="s1") is not None
    plugin._DEGRADED.clear()


def test_backstops_registered_and_restore(env):
    plugin, ctx, _, cloak = env
    _send(ctx, {"messages": [{"role": "user", "content": "שירה לוי 050-1234567"}]})
    (tool_mw,) = ctx.middleware["tool_request"]
    res = tool_mw(tool_name="send_message",
                  args={"to": "⟦טלפון_1⟧", "n": [{"who": "⟨לקוח_1⟩"}], "code": "x = arr[לקוח_1]"},
                  original_args={}, session_id="s1")
    # tool args can be code: unambiguous brackets are restored tolerantly, [..] is left alone
    assert res["args"] == {"to": "050-1234567", "n": [{"who": "שירה לוי"}], "code": "x = arr[לקוח_1]"}
    assert tool_mw(tool_name="x", args={"a": "plain"}) is None            # untouched → None
    (hook,) = ctx.middleware["hook:transform_llm_output"]
    assert hook(response_text="done for ⟦לקוח_1⟧", session_id="s1") == "done for שירה לוי"
    assert hook(response_text="nothing to do", session_id="s1") is None
    st = plugin.status()
    log = (cloak / "audit.log").read_text(encoding="utf-8")
    assert "backstop_restore" in log and '\\"self_test\\": \\"ok\\"' in log


# ---- the directory-plugin wrapper and the managed-runtime check

def test_repo_root_is_a_hermes_directory_plugin():
    import importlib.util
    import os
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    man = yaml.safe_load(open(os.path.join(root, "plugin.yaml"), encoding="utf-8"))
    assert man["name"] == "hermescloak"
    # hermes imports <plugin>/__init__.py under a synthetic package name with the plugin dir as its path
    spec = importlib.util.spec_from_file_location("hermes_plugins.hermescloak", os.path.join(root, "__init__.py"),
                                                  submodule_search_locations=[root])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from hermescloak.hermes_plugin import register as real
    assert mod._impl() is real


def _verify_mod():
    import importlib.util
    import os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     "install", "apply_hooks.py")
    spec = importlib.util.spec_from_file_location("apply_hooks_under_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fake_runtime(home, importable):
    import os
    import stat
    py = home / "installs" / "k" / "environments" / "g" / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nexit %d\n" % (0 if importable else 1))
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    return py


def test_verify_probes_the_managed_runtime_not_this_python(tmp_path, monkeypatch, capsys):
    m = _verify_mod()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _fake_runtime(tmp_path, importable=False)
    assert m._check_runtime({}) is False
    out = capsys.readouterr().out
    assert "MISSING" in out and "managed runtime" in out and "hermes plugins install" in out


def test_verify_ok_when_the_managed_runtime_imports_it(tmp_path, monkeypatch, capsys):
    m = _verify_mod()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _fake_runtime(tmp_path, importable=True)
    assert m._check_runtime({}) is True
    assert "[OK ] hermescloak loads in hermes' managed runtime" in capsys.readouterr().out


def test_verify_flags_host_isolation(tmp_path, monkeypatch, capsys):
    m = _verify_mod()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _fake_runtime(tmp_path, importable=True)
    assert m._check_runtime({"plugins": {"isolation": "host"}}) is False
    assert "plugins.isolation: host" in capsys.readouterr().out


def _root_plugin_module():
    import importlib.util
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location("hermes_plugins.hermescloak_t", os.path.join(root, "__init__.py"),
                                                  submodule_search_locations=[root])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FailsafeCtx:
    def __init__(self):
        self.mw = {}

    def register_middleware(self, kind, fn):
        self.mw.setdefault(kind, []).append(fn)

    def register_hook(self, *a, **k):
        pass


@pytest.mark.parametrize("mode,withheld", [("enforce", True), ("shadow", False), ("off", False)])
def test_failed_load_is_fail_closed_only_in_enforce(tmp_path, monkeypatch, mode, withheld):
    mod = _root_plugin_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "cloak").mkdir()
    (tmp_path / "cloak" / "MODE").write_text(mode)

    def boom():
        raise ImportError("No module named 'yaml'")
    monkeypatch.setattr(mod, "_impl", boom)
    ctx = _FailsafeCtx()
    mod.register(ctx)
    [mw] = ctx.mw["llm_request"]
    req = {"model": "m", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "דנה לוי 050-1234567"}],
           "tools": [{"type": "function"}]}
    out = mw(request=req, api_mode="chat_completions")
    log = (tmp_path / "cloak" / "audit.log").read_text(encoding="utf-8")
    assert "plugin_load_failed" in log
    if withheld:
        sent = json.dumps(out["request"], ensure_ascii=False)
        assert "דנה" not in sent and "050-1234567" not in sent and "tools" not in out["request"]
        assert out["request"]["messages"][0]["content"].startswith("[HermesCloak: request withheld")
        assert "blocked_send" in log
    else:
        assert out is None


def test_failsafe_keeps_each_wire_shape():
    mod = _root_plugin_module()
    a = mod._withheld({"model": "m", "system": [{"type": "text", "text": "x"}], "messages": [{"role": "user", "content": "y"}]},
                      "anthropic_messages")
    assert a["system"][0]["text"].startswith("[HermesCloak") and a["messages"][0]["role"] == "user"
    r = mod._withheld({"model": "m", "instructions": "x", "input": [{"role": "user", "content": "y"}]}, "codex_responses")
    assert r["instructions"].startswith("[HermesCloak") and r["input"][0]["content"].startswith("[HermesCloak") \
        and "messages" not in r
    b = mod._withheld({"modelId": "m", "system": [{"text": "x"}], "messages": [{"role": "user", "content": [{"text": "y"}]}]},
                      "bedrock_converse")
    assert b["messages"][0]["content"][0]["text"].startswith("[HermesCloak") and b["system"][0]["text"].startswith("[HermesCloak")


def test_working_load_registers_the_real_plugin_not_the_failsafe(tmp_path, monkeypatch):
    mod = _root_plugin_module()
    got = []
    monkeypatch.setattr(mod, "_impl", lambda: (lambda ctx: got.append(ctx)))
    ctx = _FailsafeCtx()
    mod.register(ctx)
    assert got == [ctx] and "llm_request" not in ctx.mw


def test_failsafe_also_withholds_auxiliary_calls(tmp_path, monkeypatch):
    """A real run found the session-title call (auxiliary funnel, no llm_request middleware) leaking
    while only the main turn was withheld."""
    import sys, types
    mod = _root_plugin_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "cloak").mkdir()
    (tmp_path / "cloak" / "MODE").write_text("enforce")
    seen = []
    ac = types.ModuleType("agent.auxiliary_client")
    ac._relay_sync_completion = lambda client, kwargs, *a, **k: seen.append(kwargs) or "ok"

    async def _a(client, kwargs, *a, **k):
        seen.append(kwargs)
        return "ok"
    ac._relay_async_completion = _a
    agent = types.ModuleType("agent"); agent.__path__ = []
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", ac)
    monkeypatch.setattr(mod, "_impl", lambda: (_ for _ in ()).throw(ImportError("x")))
    mod.register(_FailsafeCtx())
    ac._relay_sync_completion(None, {"messages": [{"role": "user", "content": "דנה לוי"}]})
    import asyncio
    asyncio.run(ac._relay_async_completion(None, {"messages": [{"role": "user", "content": "דנה לוי"}]}))
    assert len(seen) == 2 and all("דנה" not in json.dumps(k, ensure_ascii=False) for k in seen)


def test_cli_command_is_registered_and_dispatches(monkeypatch):
    from hermescloak.hermes_plugin import cli
    got = {}

    class C:
        def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
            got.update(name=name, setup=setup_fn, handler=handler_fn)
    assert cli.register(C()) == "ok" and got["name"] == "hermescloak"
    import argparse
    p = argparse.ArgumentParser()
    got["setup"](p)
    calls = []
    monkeypatch.setattr(cli, "_run", lambda script, *a: calls.append((script, a)) or 0)
    monkeypatch.setattr(cli, "_hermes_root", lambda: "/h")
    assert got["handler"](p.parse_args(["test"])) == 0 and got["handler"](p.parse_args(["verify", "--strict"])) == 0
    assert calls == [("e2e_check.py", ("--provider", "openai", "--hermes-root", "/h")),
                     ("apply_hooks.py", ("--verify", "--strict", "--hermes-root", "/h"))]
    assert cli._plugin_root() is not None           # the scripts ship in the plugin directory


def test_plugin_manifest_never_joins_the_shared_runtime():
    """python_runtime: external — hermes' PM must not treat the repo's pyproject as a dependency member
    (15.10.2026 it rebuilt the runtime shared by every agent with HermesCloak inside)."""
    import os, yaml
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert yaml.safe_load(open(os.path.join(root, "plugin.yaml"), encoding="utf-8"))["python_runtime"] == "external"


def test_wrapper_loads_its_own_copy_without_touching_sys_path():
    import sys
    before = list(sys.path)
    mod = _root_plugin_module()
    pkg = mod._load_package()
    import os
    assert os.path.dirname(pkg.__file__) == os.path.join(mod._HERE, "hermescloak")
    assert sys.path == before


def test_verify_reports_whether_the_shared_runtime_carries_a_copy(tmp_path, monkeypatch, capsys):
    m = _verify_mod()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    py = _fake_runtime(tmp_path, importable=True)
    sp = py.parent.parent / "lib" / "python3.14" / "site-packages"
    sp.mkdir(parents=True)
    assert m._check_runtime({}) is True
    assert "contains no hermescloak package" in capsys.readouterr().out
    (sp / "hermescloak").mkdir()
    (sp / "hermescloak-0.2.0.dist-info").mkdir()
    m._check_runtime({})
    out = capsys.readouterr().out
    assert "[WARN] the SHARED runtime" in out and "hermes pm install" in out
