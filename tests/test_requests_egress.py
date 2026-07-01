"""HTTP-layer egress restore: the body a script POSTs to an external API gets its
⟦tokens⟧ restored before it leaves — covering ad-hoc scripts with no shared mailer."""
import os
import sys
import types

import pytest

from hermescloak.durable_vault import DurableVault


def _seed(home, real, etype):
    vdir = os.path.join(home, "cloak", "vaults"); os.makedirs(vdir, exist_ok=True)
    v = DurableVault(DurableVault.path_for(vdir, home))
    tok = v.tokenize(real, etype); v.save()
    return tok


@pytest.fixture
def fake_requests(monkeypatch):
    """Install a stand-in `requests` module that records the final body, then patch it."""
    sent = {}
    mod = types.ModuleType("requests")
    sessions = types.ModuleType("requests.sessions")

    class Session:
        def request(self, method, url, **kwargs):
            sent["url"] = url; sent["json"] = kwargs.get("json"); sent["data"] = kwargs.get("data")
            return {"status": 202}

    sessions.Session = Session
    mod.sessions = sessions
    monkeypatch.setitem(sys.modules, "requests", mod)
    monkeypatch.setitem(sys.modules, "requests.sessions", sessions)
    # fresh install each test
    import hermescloak.integrations.requests_egress as re_mod
    re_mod._INSTALLED = False
    return mod, sessions, sent, re_mod


def test_restores_json_body_before_send(tmp_path, fake_requests):
    home = str(tmp_path)
    tok = _seed(home, "100000009", "מזהה")
    mod, sessions, sent, re_mod = fake_requests
    assert re_mod.install(home) is True

    L, R = chr(0x27E6), chr(0x27E7)                       # the chr()-built token, as in the field bug
    body = {"message": {"body": {"content": f"ת\"ז: {L}מזהה_1{R}"}}}
    sessions.Session().request("POST", "https://graph.microsoft.com/v1.0/sendMail", json=body)

    blob = str(sent["json"])
    assert "100000009" in blob and "⟦" not in blob       # restored in the outbound body


def test_restores_str_data_body(tmp_path, fake_requests):
    home = str(tmp_path)
    _seed(home, "050-1234567", "טלפון")
    mod, sessions, sent, re_mod = fake_requests
    re_mod.install(home)
    sessions.Session().request("POST", "https://api.example.com/x", data="phone=⟦טלפון_1⟧")
    assert "050-1234567" in sent["data"] and "⟦" not in sent["data"]


def test_untouched_when_no_token(tmp_path, fake_requests):
    home = str(tmp_path); _seed(home, "x", "לקוח")
    mod, sessions, sent, re_mod = fake_requests
    re_mod.install(home)
    body = {"message": "no tokens here"}
    sessions.Session().request("POST", "https://x/y", json=body)
    assert sent["json"] == body                           # unchanged object/content


def test_restores_base64_mime_calendar_body(tmp_path, fake_requests):
    """Graph MIME sendMail base64-encodes the whole MIME (text/calendar with a phone token)
    into data= — the literal ⟦ is hidden. The patch must decode, restore, re-encode."""
    import base64
    home = str(tmp_path)
    _seed(home, "050-1234567", "טלפון")
    mod, sessions, sent, re_mod = fake_requests
    re_mod.install(home)
    L, R = chr(0x27E6), chr(0x27E7)
    mime = ('Content-Type: text/calendar; method="REQUEST"\r\n\r\n'
            'BEGIN:VCALENDAR\r\nDESCRIPTION:ליצירת קשר ' + L + 'טלפון_1' + R + '\r\nEND:VCALENDAR')
    encoded = base64.b64encode(mime.encode("utf-8"))          # bytes, exactly like calendar_mime_graph.py
    sessions.Session().request("POST", "https://graph.microsoft.com/v1.0/users/x/sendMail",
                               headers={"Content-Type": "text/plain"}, data=encoded)
    out = base64.b64decode(sent["data"]).decode("utf-8")      # what really went out, decoded back
    assert "050-1234567" in out and "⟦" not in out            # phone restored inside the base64 MIME


def test_kill_switch(tmp_path, fake_requests, monkeypatch):
    home = str(tmp_path); _seed(home, "100000009", "מזהה")
    mod, sessions, sent, re_mod = fake_requests
    monkeypatch.setenv("HERMESCLOAK_EGRESS_OFF", "1")
    assert re_mod.install(home) is False                  # disabled → not patched
    sessions.Session().request("POST", "https://x", json={"c": "⟦מזהה_1⟧"})
    assert sent["json"] == {"c": "⟦מזהה_1⟧"}              # token left as-is (patch off)
